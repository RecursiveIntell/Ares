"""Real SDK/loopback + native governor + daemon + native ledger verification.

Disposable source layout exercises the real source owner, not live activation.
No credentials or external provider calls. Native binaries are test inputs.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from ares_runtime.physical_dispatch import inference_call_scope
from tests.ares_runtime.test_continuity_input import db, accept  # noqa: F401
from tests.ares_runtime.test_physical_http_transport import endpoint, client  # noqa: F401


def _native_inputs():
    token = os.environ.get('RA_DAEMON_BIN')
    if token is None:
        pytest.skip('RA_DAEMON_BIN is unset; native integration requires an explicit qualified artifact')
    binary = Path(token)
    assert token and binary.is_file() and os.access(binary, os.X_OK), 'explicit RA_DAEMON_BIN is not executable'
    native = binary.with_name('ra')
    assert native.is_file() and os.access(native, os.X_OK), 'paired native verifier is unavailable'
    return binary, native


@pytest.mark.parametrize('mode', ['enforce','observe'])
def test_real_physical_send_native_receipt_and_cli_verification(db, tmp_path, endpoint, monkeypatch, mode):
    binary, native = _native_inputs()
    source = Path(__file__).resolve().parents[3]/'recursive-agent/integrations/hermes-native'
    name = 'native_transport_integration_'+mode
    spec = importlib.util.spec_from_file_location(name, source/'__init__.py', submodule_search_locations=[str(source)])
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    governor = importlib.import_module(name+'.governor')
    from hermes_cli import middleware
    from ares_runtime import local_runtime
    rpc_failures=[]
    original_settle=governor.NativeGovernanceClient.attempt_settle
    def traced_settle(self,*args,**kwargs):
        try:
            return original_settle(self,*args,**kwargs)
        except Exception as error:
            rpc_failures.append((type(error).__name__,getattr(error,'code',None),str(error),args[-1]))
            raise
    monkeypatch.setattr(governor.NativeGovernanceClient,'attempt_settle',traced_settle)

    # Actual source/release descriptor and persisted input-turn lease owner.
    repo = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    installed_source = tmp_path/'release'/revision/'source'
    subprocess.run(['git','clone','--shared','--quiet',str(repo),str(installed_source)],check=True,timeout=30)
    (installed_source.parent/'release.json').write_text(json.dumps({'revision':revision,'runtime_binding':{
        'schema':'AresLocalRuntimeBindingV1','source':str(installed_source),'controller_contract':local_runtime.LOCAL_LIFECYCLE_CONTRACT}}))
    monkeypatch.setattr(local_runtime,'__file__',str(installed_source/'ares_runtime/local_runtime.py'))
    receipt = accept(db)
    db.begin_context_input_turn('s',receipt=receipt,turn_lease_holder='holder')
    agent=SimpleNamespace(session_id='s',_session_db=db,_active_session_turn_lease_holder='holder',
                          provider='openai',model='local',api_mode='chat_completions')
    url,bodies,_ = endpoint
    home = tmp_path/'home'; home.mkdir(mode=0o700)
    monkeypatch.setattr(governor,'get_hermes_home',lambda: home)
    grant = json.loads(subprocess.check_output([str(binary),'emit-observe-egress-grant','--ares-home',str(home),
        '--expires-at',(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()],text=True))
    grant.update(mode=mode,any_route=mode=='observe',routes=[] if mode=='observe' else [dict(
        provider_identity='openai_compatible:'+url+'/chat/completions',model_ref='local',api_mode='chat_completions')])
    material=dict(grant); material.pop('policy_digest')
    grant['policy_digest']='sha256:'+hashlib.sha256(json.dumps(material,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    grant_file=home/'grant.json';grant_file.write_text(json.dumps(grant));grant_file.chmod(0o600)
    socket_path=tmp_path/'n.sock';runs=home/'runs'
    callbacks={};hooks={}
    ctx=SimpleNamespace(get_config=lambda key,default=None: {'governance_mode':mode,'socket_path':str(socket_path)}.get(key,default),
        register_middleware=lambda key,callback:callbacks.update({key:callback}),
        register_hook=lambda key,callback:hooks.update({key:callback}))
    with (tmp_path/'native.log').open('w+') as log:
        daemon=subprocess.Popen([str(binary),'serve','--root',str(runs),'--socket',str(socket_path),
            '--ares-home',str(home),'--egress-grant-file',str(grant_file)],stdout=subprocess.DEVNULL,stderr=log)
        try:
            deadline=time.monotonic()+10
            while not socket_path.exists():
                if daemon.poll() is not None or time.monotonic()>deadline:
                    log.seek(0);pytest.fail(log.read())
                time.sleep(.02)
            owner=governor.register_governor(ctx)
            monkeypatch.setattr(middleware,'_get_middleware_callbacks',lambda _:list(callbacks.values()))
            payload={'model':'local','messages':[{'role':'user','content':'private fixture prompt'}],'max_tokens':7}
            try:
                with client(url) as sdk,inference_call_scope(agent,payload):
                    result=sdk.chat.completions.create(**payload)
            except Exception as failure:
                chain=[]; current=failure
                while current is not None:
                    chain.append((type(current).__name__,getattr(current,'code',None),str(current)))
                    current=current.__cause__
                log.flush();log.seek(0)
                pytest.fail(repr(chain)+'\n'+repr(rpc_failures)+'\n'+log.read())
            assert result.choices[0].message.content=='ok' and len(bodies)==1
            assert len(owner.sessions)==1
            hooks['on_session_finalize'](session_id='s')
            assert not (home/'recursive-agent/unwitnessed.ndjson').exists()
            ledgers=list(runs.rglob('receipts.ndjson'))
            assert len(ledgers)==2
            all_records=[]
            for ledger in ledgers:
                check=subprocess.run([str(native),'verify','--run',str(ledger.parent)],capture_output=True,text=True,timeout=20)
                assert check.returncode==0,check.stdout+check.stderr
                all_records.extend(json.loads(row) for row in ledger.read_text().splitlines())
            assert sum(r['kind']=='run_finalized' for r in all_records)==2
            artifact_bytes=b'\n'.join(p.read_bytes() for p in runs.rglob('*') if p.is_file())
            assert b'ares.transport-response-body/v1' in artifact_bytes
            assert b'private fixture prompt' not in artifact_bytes
        finally:
            daemon.terminate();daemon.wait(timeout=10)
