import json
import sys

from ghostdev.cli import main
from ghostdev.config import Settings


def test_migration_preserves_project_and_phone_settings(tmp_path, monkeypatch, capsys):
    path=tmp_path/'config.json'
    previous=Settings().model_dump(mode='json')
    previous.update(model_url='http://192.168.1.5:11434',model_name='old-remote-model',model_publisher='NVIDIA',origins=['https://my-mac.example.invalid'],state_dir='saved-tasks')
    path.write_text(json.dumps(previous))
    monkeypatch.setattr(sys,'argv',['ghostdev','--config',str(path),'use-local-model'])
    main()
    revised=Settings.model_validate_json(path.read_text())
    assert revised.model_url=='http://127.0.0.1:11434' and revised.model_name=='qwen3:4b'
    assert revised.origins==previous['origins'] and str(revised.state_dir)=='saved-tasks'
    assert 'preserved' in capsys.readouterr().out
