"""Static census is a declared CLI gate, tested on disposable source only."""
from scripts.provider_call_census import census_source, qualify_rows


def test_nested_sdk_and_native_calls_have_stable_semantic_keys():
    rows = census_source('agent/example.py', '''
class Client:
    def send(self):
        c.chat.completions.create(model="x")
        c.chat.completions.create(model="y")
        self._native.call("x")
''')
    assert {(r['function'], r['pattern'], r['occurrences']) for r in rows} == {
        ('Client.send', 'chat.completions.create', 2), ('Client.send', '_native.call', 1)}
    assert all(r['classification'] == 'unrouted' for r in qualify_rows(rows, []))


def test_exact_fixture_does_not_blanket_exclude_unknown_sibling():
    rows = census_source('agent/example.py', 'def first():\n c.responses.create()\ndef second():\n c.responses.create()\n')
    fixture = [{'file':'agent/example.py','function':'first','pattern':'responses.create',
                'classification':'main_physical','evidence':'test-boundary'}]
    assert [r['classification'] for r in qualify_rows(rows, fixture)] == ['main_physical', 'unrouted']



def test_import_alias_is_not_a_constructor_census_escape():
    rows=census_source('agent/fixture.py','from openai import OpenAI as Hidden\ndef f():\n return Hidden()\n')
    assert [(r['function'],r['pattern']) for r in rows]==[('f','OpenAI')]


def test_cli_detects_unknown_sibling_count_drift_and_stale_fixture(tmp_path):
    import json,subprocess,sys
    from pathlib import Path
    script=Path(__file__).resolve().parents[2]/'scripts/provider_call_census.py'
    root=tmp_path/'source';(root/'agent').mkdir(parents=True)
    source=root/'agent/example.py'
    source.write_text('def first():\n c.responses.create()\n')
    fixture=tmp_path/'fixture.json'
    row={'file':'agent/example.py','function':'first','pattern':'responses.create','occurrences':1,
         'classification':'main_physical','evidence':'disposable-CLI-fixture'}
    fixture.write_text(json.dumps([row]))
    output=tmp_path/'output.json'
    def invoke():
        result=subprocess.run([sys.executable,str(script),'--root',str(root),'--fixture',str(fixture),
            '--output',str(output),'--check'],capture_output=True,text=True,timeout=10)
        return result,json.loads(output.read_text())
    result,document=invoke()
    assert result.returncode==0 and document['unqualified']==0
    source.write_text(source.read_text()+'def second():\n c.responses.create()\n')
    result,document=invoke()
    assert result.returncode==1 and document['unqualified']==1
    source.write_text('def first():\n c.responses.create()\n c.responses.create()\n')
    result,document=invoke()
    assert result.returncode==1 and document['unqualified']==1
    assert document['rows'][0]['occurrences']==2
    source.write_text('')
    result,document=invoke()
    assert result.returncode==1 and len(document['stale_fixture'])==1
