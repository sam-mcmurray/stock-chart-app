"""Regression checks for preservation and publication boundaries; no live services."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load('publication_guard', 'publication_guard.py')
setup = load('check_starter', 'check-starter.py')


class PublicationTools(unittest.TestCase):
    def test_private_paths_and_legitimate_source(self):
        for name in ['backend/PROGRESS_AND_GOALS.md', 'frontend/PROGRESS_AND_GOALS.md',
                     'docs/my-plan.md', 'config.yaml', 'data/ollama-news/id_ed25519',
                     '.starter/backend.env', 'backend/.env', 'certs/server.key']:
            self.assertTrue(guard.private_path(name), name)
        for name in ['backend/drl_agent/candidate_gap_plan.py', 'backend/.env.example', 'README.md']:
            self.assertFalse(guard.private_path(name), name)

    def test_existing_starter_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.starter').mkdir()
            sentinel = root / '.starter/backend.env'
            sentinel.write_text('KEEP EXISTING DATA')
            with patch.object(setup, 'ROOT', root), patch.object(setup.shutil, 'which', return_value='/bin/openssl'):
                with self.assertRaises(FileExistsError):
                    setup.initialize()
            self.assertEqual(sentinel.read_text(), 'KEEP EXISTING DATA')

    def test_export_excludes_private_data_and_symlinks_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'app'
            root.mkdir()
            for name, value in {'README.md': '# Demo', 'backend/main.py': 'print("hello")',
                                'backend/PROGRESS_AND_GOALS.md': 'private goals',
                                'backend/.env': 'SCHWAB_CLIENT_SECRET=private-demo-secret-12345',
                                'docker-compose.yml': 'private local topology',
                                'compose.starter.yaml': 'name: starter',
                                'data/secret.txt': 'private data'}.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            (root / 'backend/link.py').symlink_to(root / 'backend/.env')
            dest = Path(tmp) / 'public'
            with patch.object(guard, 'ROOT', root):
                guard.export(dest)
                with self.assertRaises(RuntimeError):
                    guard.export(dest)
            self.assertEqual({str(p.relative_to(dest)) for p in dest.rglob('*') if p.is_file()},
                             {'README.md', 'backend/main.py', 'compose.starter.yaml', 'PUBLICATION_MANIFEST.json'})

    def test_known_secret_blocks_export_before_destination_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'app'
            (root / 'backend').mkdir(parents=True)
            (root / 'README.md').write_text('private-demo-secret-12345')
            (root / 'backend/.env').write_text('SCHWAB_CLIENT_SECRET=private-demo-secret-12345')
            dest = Path(tmp) / 'public'
            with patch.object(guard, 'ROOT', root):
                with self.assertRaises(RuntimeError):
                    guard.export(dest)
            self.assertFalse(dest.exists())

    def test_deleted_private_file_in_history_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            git('init')
            git('config', 'user.name', 'Synthetic Test')
            git('config', 'user.email', 'test@example.invalid')
            (root / 'config.yaml').write_text('private: test')
            git('add', 'config.yaml')
            git('commit', '-m', 'synthetic private fixture')
            git('rm', 'config.yaml')
            git('commit', '-m', 'remove current file')
            with patch.object(guard, 'ROOT', root):
                self.assertEqual(guard.check_history(False), 0)
                self.assertEqual(guard.check_history(True), 1)


    def test_alias_in_old_tree_cannot_hide_private_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(['git', '-C', str(root), *args], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            git('init'); git('config', 'user.name', 'Synthetic Test')
            git('config', 'user.email', 'test@example.invalid')
            (root / 'public.md').write_text('identical bytes')
            (root / 'PROGRESS_AND_GOALS.md').write_text('identical bytes')
            git('add', '.'); git('commit', '-m', 'same blob under two paths')
            git('rm', 'PROGRESS_AND_GOALS.md'); git('commit', '-m', 'remove private alias')
            with patch.object(guard, 'ROOT', root):
                self.assertEqual(guard.check_history(True), 1)

    def test_portal_capture_and_short_account_number_are_excluded(self):
        self.assertTrue(guard.private_path('docs/schwab/source/portal.json'))
        self.assertTrue(guard.private_path('docs/progress.json'))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'app'
            (root / 'backend').mkdir(parents=True)
            (root / 'backend/.env').write_text('SCHWAB_ACCOUNT_NUMBER=98765432')
            (root / 'README.md').write_text('account: 98765432')
            with patch.object(guard, 'ROOT', root):
                with self.assertRaises(RuntimeError):
                    guard.export(Path(tmp) / 'out')

    def test_export_uses_scanned_bytes_during_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'app'
            root.mkdir()
            (root / 'README.md').write_text('# Public text')
            def change_source(data, values):
                (root / 'README.md').write_text('changed after snapshot')
                return False
            with patch.object(guard, 'ROOT', root), patch.object(guard, 'sensitive', side_effect=change_source):
                guard.export(Path(tmp) / 'out')
            self.assertEqual((Path(tmp) / 'out/README.md').read_text(), '# Public text')

    def test_broken_public_link_blocks_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'app'
            root.mkdir()
            (root / 'README.md').write_text('[Private](PROGRESS_AND_GOALS.md)')
            destination = Path(tmp) / 'out'
            with patch.object(guard, 'ROOT', root):
                with self.assertRaises(RuntimeError):
                    guard.export(destination)
            self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
