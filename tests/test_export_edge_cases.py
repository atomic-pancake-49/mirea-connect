"""Additional export boundaries: cancellation, write failure and accounting."""
import asyncio
import io
import json
import zipfile

import httpx
import pytest

from mirea_connect.materials import CourseRef, ExportLimits, export_materials

COURSE = '<main id="region-main">' + ''.join(
    f'<li class="activity modtype_resource" id="module-{i}"><a href="/mod/resource/view.php?id={i}">File {i}</a></li>'
    for i in range(1, 4)
) + '</main>'


def handler(request):
    if request.url.path == '/course/view.php':
        return httpx.Response(200, text=COURSE, headers={'content-type': 'text/html'})
    return httpx.Response(200, content=b'x' * 100, headers={'content-type': 'application/pdf', 'content-disposition': 'attachment; filename="file.pdf"'})


async def test_cancel_keeps_completed_entries_and_finalizes_zip(tmp_path):
    cancel = asyncio.Event()

    def progress(event):
        if event.get('items_done', 0) >= 1:
            cancel.set()

    path = tmp_path / 'cancelled.zip'
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with path.open('xb') as output:
            result = await export_materials(client, [CourseRef('1', 'A')], scope='all', output=output, cancel=cancel, progress=progress)
            assert not output.closed
    assert result.status == 'cancelled'
    assert result.zip_bytes == b''
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert len([i for i in archive.namelist() if i.endswith('.pdf')]) == 1
        assert json.loads(archive.read('manifest.json'))['status'] == 'cancelled'


async def test_exception_still_finalizes_completed_entries(tmp_path):
    def progress(event):
        if event.get('items_done', 0) >= 1:
            raise RuntimeError('synthetic interruption')

    path = tmp_path / 'interrupted.zip'
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with path.open('xb') as output:
            with pytest.raises(RuntimeError, match='interruption'):
                await export_materials(client, [CourseRef('1', 'A')], scope='all', output=output, progress=progress)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert any(i.endswith('.pdf') for i in archive.namelist())


async def test_zero_material_cap_still_has_explicit_bounded_metadata():
    limits = ExportLimits(max_total_bytes=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await export_materials(client, [CourseRef('1', 'A')], scope='all', limits=limits)
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        assert archive.testzip() is None
        assert not any(i.endswith('.pdf') for i in archive.namelist())
        assert sum(i.file_size for i in archive.infolist()) <= limits.max_metadata_bytes
    assert result.manifest['byte_budget']['material_bytes'] == 0
    assert result.status == 'partial'


async def test_course_cap_resets_but_global_cap_does_not():
    limits = ExportLimits(max_total_bytes=350, max_course_bytes=200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await export_materials(client, [CourseRef('1', 'A'), CourseRef('2', 'B')], scope='all', limits=limits)
    assert all(course['bytes'] <= 200 for course in result.manifest['courses'])
    assert result.manifest['totals']['saved_bytes'] <= 350
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        payload = sum(i.file_size for i in archive.infolist() if i.filename.endswith('.pdf'))
    assert payload == result.manifest['totals']['saved_bytes']


async def test_write_error_is_propagated_not_marked_complete():
    class Broken(io.BytesIO):
        def write(self, data):
            raise OSError('synthetic disk full')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        output = Broken()
        with pytest.raises(OSError):
            await export_materials(client, [CourseRef('1', 'A')], scope='all', output=output)


def test_normal_names_are_not_changed_by_redaction():
    from mirea_connect.materials import safe_component
    assert safe_component('Лекция 20% и 100%') == 'Лекция 20% и 100%'
