"""Disk-backed production ZIP and explicit payload-budget contract."""
import io
import zipfile

import httpx

from mirea_connect.materials import CourseRef, ExportLimits, export_materials


async def test_archive_writes_to_output_stream_not_returned_bytes(tmp_path):
    course='<main id="region-main"><li class="activity modtype_page" id="module-1"><a href="/mod/page/view.php?id=1">Page</a></li></main>'
    def handle(request):
        return httpx.Response(200,text=course if request.url.path=='/course/view.php' else '<main id="region-main">Text</main>',headers={'content-type':'text/html'})
    target=tmp_path/'output.zip'
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with target.open('xb') as output:
            result=await export_materials(client,[CourseRef('1','Synthetic')],scope='course:1',output=output)
    assert result.zip_bytes==b''
    assert result.status=='complete'
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert 'manifest.json' in archive.namelist()
        assert any(n.endswith('.md') for n in archive.namelist())


async def test_explicit_total_limit_includes_all_payload_bytes():
    course='<main id="region-main"><li class="activity modtype_page" id="module-1"><a href="/mod/page/view.php?id=1">Page</a></li></main>'
    def handle(request):
        return httpx.Response(200,text=course if request.url.path=='/course/view.php' else '<main id="region-main">'+('word '*200)+'</main>',headers={'content-type':'text/html'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result=await export_materials(client,[CourseRef('1','Synthetic')],scope='course:1',limits=ExportLimits(max_total_bytes=500))
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as archive:
        # Explicit material budget, bounded service metadata is reported separately.
        payload=sum(i.file_size for i in archive.infolist() if i.filename!='manifest.json' and not i.filename.endswith('/README.md'))
    assert payload<=500
    assert result.status=='partial'


def test_default_has_no_overall_course_or_archive_cap():
    limits=ExportLimits()
    assert limits.max_total_bytes is None
    assert limits.max_course_bytes is None
