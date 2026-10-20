"""Controlled same-process read concurrency; no model or live home."""
from concurrent.futures import ThreadPoolExecutor
import threading

from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations


def test_concurrent_local_reads_each_use_one_launch_and_preserve_bytes(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('terminal:\n  backend: local\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(home))
    fixture = tmp_path / 'fixture.txt'
    fixture.write_text('alpha\nbeta\ngamma\n', encoding='utf-8')
    owners = [LocalEnvironment(cwd=str(tmp_path), timeout=30) for _ in range(4)]
    counts = {id(owner): 0 for owner in owners}
    lock = threading.Lock()
    original = LocalEnvironment.execute

    def execute(owner, *args, **kwargs):
        with lock:
            counts[id(owner)] += 1
        return original(owner, *args, **kwargs)

    monkeypatch.setattr(LocalEnvironment, 'execute', execute)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(ShellFileOperations(owner).read_file,
                                   str(fixture), 1, 3) for owner in owners]
            results = [future.result(timeout=45) for future in futures]
        for result in results:
            assert result.error is None
            assert result.content == '1|alpha\n2|beta\n3|gamma\n4|'
            assert result.total_lines == 3
            assert result.truncated is False
        assert all(count == 1 for count in counts.values())
    finally:
        for owner in owners:
            owner.cleanup()
