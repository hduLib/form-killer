from __future__ import annotations

from form_killer import config


def test_default_storage_paths_are_project_local(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)

    assert config.get_config_path() == tmp_path / ".form-killer" / "config.toml"
    assert config.get_legacy_config_path() == tmp_path / ".form-killer" / "config.json"
    assert config.get_default_tencent_session_path() == tmp_path / ".form-killer" / "sessions" / "tencent.json"
    assert config.get_default_wps_session_path() == tmp_path / ".form-killer" / "sessions" / "wps.json"


def test_project_storage_can_be_overridden(monkeypatch, tmp_path) -> None:
    project_dir = tmp_path / "project"
    monkeypatch.setenv("FORM_KILLER_PROJECT_DIR", str(project_dir))

    assert config.get_config_path() == project_dir.resolve() / ".form-killer" / "config.toml"


def test_save_and_load_base_url(monkeypatch, tmp_path) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)

    target = config.save_base_url("https://llm.example.test/v1/")

    assert target == str(path)
    assert config.load_base_url() == "https://llm.example.test/v1"


def test_save_and_load_model(monkeypatch, tmp_path) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)

    target = config.save_model("gpt-test")

    assert target == str(path)
    assert config.load_model() == "gpt-test"


def test_project_api_key_preserves_base_url(monkeypatch, tmp_path) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)

    config.save_base_url("https://llm.example.test/v1")
    config.save_api_key("test-key")

    assert config.load_base_url() == "https://llm.example.test/v1"
    assert config.load_api_key() == "test-key"


def test_clear_api_key_preserves_base_url(monkeypatch, tmp_path) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-missing"))

    config.save_base_url("https://llm.example.test/v1")
    path.write_text(
        'openai_api_key = "bad-key"\nopenai_base_url = "https://llm.example.test/v1"\n',
        encoding="utf-8",
    )

    cleared = config.clear_api_key()

    assert cleared == [str(path)]
    assert config.load_api_key() is None
    assert config.load_base_url() == "https://llm.example.test/v1"


def test_load_api_key_with_source(monkeypatch, tmp_path) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    path.write_text('openai_api_key = "saved-key"\n', encoding="utf-8")

    assert config.load_api_key_with_source() == ("saved-key", "config")


def test_load_api_key_falls_back_to_codex_auth(monkeypatch, tmp_path) -> None:
    project_config = tmp_path / "missing.toml"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"OPENAI_API_KEY": "codex-key"}', encoding="utf-8")
    monkeypatch.setattr(config, "get_config_path", lambda: project_config)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert config.load_api_key_with_source() == ("codex-key", "codex")


def test_load_codex_openai_defaults(monkeypatch, tmp_path) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model_provider = "OpenAI"\nmodel = "gpt-codex"\n[model_providers.OpenAI]\nbase_url = "https://llm.example.test/"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert config.load_codex_base_url() == "https://llm.example.test"
    assert config.load_codex_model() == "gpt-codex"


def test_session_paths_expand_environment_variables(monkeypatch, tmp_path) -> None:
    path = tmp_path / "config.toml"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    monkeypatch.setenv("FORM_KILLER_TEST_DATA", str(data_dir))
    path.write_text('tencent_session_path = "$FORM_KILLER_TEST_DATA/tencent.json"\n', encoding="utf-8")

    assert config.load_tencent_session_path() == data_dir / "tencent.json"


def test_legacy_json_config_is_still_read(monkeypatch, tmp_path) -> None:
    path = tmp_path / "config.toml"
    legacy_path = tmp_path / "config.json"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    monkeypatch.setattr(config, "get_legacy_config_path", lambda: legacy_path)
    legacy_path.write_text('{"openai_model": "legacy-model"}', encoding="utf-8")

    assert config.load_model() == "legacy-model"


def test_ensure_config_file_writes_comments(monkeypatch, tmp_path) -> None:
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "get_config_path", lambda: path)
    monkeypatch.setattr(config, "get_legacy_config_path", lambda: tmp_path / "missing.json")

    created = config.ensure_config_file()

    assert created == path
    text = path.read_text(encoding="utf-8")
    assert "# form-killer 配置文件" in text
    assert "openai_model" in text
