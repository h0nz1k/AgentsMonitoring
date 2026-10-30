import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from agentsmon import detect,wizard


class ModelIdentity(unittest.TestCase):
    def test_hermes_never_borrows_codex_model_on_missing_config(self):
        with patch.object(detect,'_hermes_model',return_value=None),patch.object(detect,'_hermes_live_model',return_value=None),patch.object(detect,'_codex_model',return_value='Wrong'),patch.object(detect,'_codex_model_any',return_value='Wrong'):
            self.assertIsNone(detect.daemon_model('Hermes'))

    def test_yaml_model_key_order_quotes_and_comments(self):
        with tempfile.TemporaryDirectory() as d,patch.object(Path,'home',return_value=Path(d)):
            p=Path(d)/'.hermes';p.mkdir()
            (p/'config.yaml').write_text('model:\n  provider: openai-codex\n  default: "gpt-6.1-sol" # model\n')
            self.assertEqual(detect._hermes_model(),'GPT-6.1 Sol')

    def test_claude_argv_ignores_helper_and_accepts_alias(self):
        self.assertEqual(detect._model_from_argv(['node helper.js --model wrong','/opt/bin/claude --model=sonnet']),'Sonnet')

    def test_precise_session_model_wins_but_ambiguous_cwd_does_not(self):
        self.assertEqual(detect.label_for_claude('Sonnet 5',['claude --model opus'],'Claude Code'),'Sonnet 5')
        self.assertEqual(detect.label_for_claude('Sonnet 5',['claude --model opus'],'Claude Code',exact_session=False),'Opus')

    def test_wizard_does_not_freeze_live_model_as_manual_override(self):
        with patch.object(detect,'daemon_model',return_value='A model'),patch.object(detect,'daemon_telegram_bot',return_value=None):
            _,pinned=wizard._daemon_entries({'name':'Hermes','pattern':'hermes','binary':'hermes'})
        self.assertNotIn('tag',pinned); self.assertNotIn('vendor',pinned)
