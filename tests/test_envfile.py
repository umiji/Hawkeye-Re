import os

from hawkeye.envfile import load_local_env


def test_loads_env_local_file(tmp_path, monkeypatch):
    monkeypatch.delenv("HAWKEYE_TEST_VAR", raising=False)
    (tmp_path / ".env.local").write_text("HAWKEYE_TEST_VAR=from_local\n",
                                         encoding="utf-8")

    load_local_env(tmp_path)

    assert os.environ["HAWKEYE_TEST_VAR"] == "from_local"


def test_shell_value_is_not_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("HAWKEYE_TEST_VAR", "from_shell")
    (tmp_path / ".env.local").write_text("HAWKEYE_TEST_VAR=from_local\n",
                                         encoding="utf-8")

    load_local_env(tmp_path)

    assert os.environ["HAWKEYE_TEST_VAR"] == "from_shell"


def test_env_local_takes_precedence_over_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HAWKEYE_TEST_VAR", raising=False)
    (tmp_path / ".env.local").write_text("HAWKEYE_TEST_VAR=from_local\n",
                                         encoding="utf-8")
    (tmp_path / ".env").write_text("HAWKEYE_TEST_VAR=from_dotenv\n",
                                   encoding="utf-8")

    load_local_env(tmp_path)

    assert os.environ["HAWKEYE_TEST_VAR"] == "from_local"


def test_missing_env_files_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("HAWKEYE_TEST_VAR", raising=False)

    load_local_env(tmp_path)

    assert "HAWKEYE_TEST_VAR" not in os.environ
