"""Startup cancellation reports a usable next step without a traceback."""
import pytest

from ott_agent.cli import main


@pytest.mark.parametrize("has_checkpoint", [False, True])
def test_ctrl_c_during_startup_returns_clean_interrupt_and_recovery_hint(tmp_path, monkeypatch, capsys, has_checkpoint):
    out = tmp_path / "interrupted-run"
    if has_checkpoint:
        out.mkdir()
        (out / "state.sqlite3").touch()

    def interrupted_load(path, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr("ott_agent.cli.Pack", interrupted_load)
    assert main(["replay", "--out", str(out)]) == 130
    captured = capsys.readouterr()
    assert "Loading source pack" in captured.out
    assert "Traceback" not in captured.err
    if has_checkpoint:
        assert "--resume" in captured.err
    else:
        assert "No replay state was created" in captured.err
        assert not out.exists()


def test_second_serve_exits_cleanly_without_touching_running_server(tmp_path,monkeypatch,capsys):
    from ott_agent.service import acquire_runtime_lock
    runtime=tmp_path/'runtime/api'
    owner=acquire_runtime_lock(runtime)
    try:
        monkeypatch.setattr('dotenv.load_dotenv',lambda *a,**k:None)
        monkeypatch.setattr('ott_agent.cli.PROJECT',tmp_path)
        monkeypatch.setattr('uvicorn.run',lambda *a,**k:pytest.fail('Second server must not launch'))
        assert main(['serve'])==1
        captured=capsys.readouterr()
        assert 'already has an owner' in captured.err
        assert 'Ctrl+C' in captured.err and 'recorded PID' in captured.err
        assert 'Traceback' not in captured.err
        assert 'Opening local review service' not in captured.out
        assert not (runtime/'jobs.sqlite3').exists()
    finally:
        owner.close()


def test_lock_file_left_after_exit_does_not_block_startup(tmp_path):
    from ott_agent.service import acquire_runtime_lock,check_runtime_available
    path=tmp_path/'server.lock'
    path.write_text('{"pid":99999999}')
    check_runtime_available(tmp_path)
    owner=acquire_runtime_lock(tmp_path)
    owner.close()
    check_runtime_available(tmp_path)
