"""Release-only checks; no application, database, provider or network is started."""
import hashlib
import ast
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_files', ROOT/'scripts/release_files.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        parent=ROOT/'.copilot/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='release-test-',dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def put(self, name, data=b'example'):
        p=self.root/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(data)
        return p

    def test_private_runtime_and_path_escape_excluded(self):
        for name in ('deploy/runtime.local-preview.json','deploy/runtime.production.json',
                     '.env.copilot','.copilot/run/state.json','backend/instance/a.json',
                     'backend/uploads/a.png','deploy/private.key','deploy/session.db',
                     '../frontend/a.ts','C:/frontend/a.ts','frontend\\a.ts',''):
            with self.subTest(name=name): self.assertFalse(release.source_allowed(name))
        for name in ('deploy/runtime.server.example.json','.env.copilot.example','LICENSE',
                     'backend/services/manual_balance.py'):
            self.assertTrue(release.source_allowed(name))

    def test_reviewed_untracked_included_without_staging(self):
        with patch.object(release,'EXTRA_SOURCE_FILES',{'backend/new.py'}), \
             patch.object(release,'git',side_effect=['backend/old.py\0','backend/new.py\0']):
            self.assertEqual(release.source_names(self.root),['backend/new.py','backend/old.py'])

    def test_unreviewed_new_source_fails_closed(self):
        with patch.object(release,'EXTRA_SOURCE_FILES',set()), \
             patch.object(release,'git',side_effect=['backend/old.py\0','backend/unknown.py\0']):
            with self.assertRaisesRegex(ValueError,'明确纳入'):
                release.source_names(self.root)

    def build_fixture(self):
        source=self.put('frontend/src/main.ts',b'current source')
        self.put('frontend/public/a.png',b'public image')
        self.put('.copilot/frontend-dist/a.png',b'public image')
        self.put('.copilot/frontend-dist/index.html',b'<script src="/assets/new.js"></script>')
        self.put('.copilot/frontend-dist/assets/new.js',b'current built js')
        self.put('.copilot/frontend-dist/assets/obsolete.js',b'must not ship')
        self.put('.copilot/frontend-dist/.vite/manifest.json',
                 json.dumps({'index.html':{'file':'assets/new.js'}}).encode())
        os.utime(source,(100,100))
        os.utime(self.root/'frontend/public/a.png',(100,100))
        return ['frontend/public/a.png','frontend/src/main.ts']

    def test_current_build_reuse_and_newer_source_rejected(self):
        names=self.build_fixture()
        self.assertEqual(release.frontend_evidence(self.root,names)['input_files'],2)
        p=self.root/'frontend/src/main.ts'
        future=(self.root/'.copilot/frontend-dist/index.html').stat().st_mtime+10
        os.utime(p,(future,future))
        with self.assertRaisesRegex(ValueError,'晚于构建'):
            release.frontend_evidence(self.root,names)

    def test_stale_public_copy_rejected(self):
        names=self.build_fixture()
        self.put('.copilot/frontend-dist/a.png',b'old image')
        with self.assertRaisesRegex(ValueError,'副本不匹配'):
            release.frontend_evidence(self.root,names)

    def test_stale_html_manifest_pair_rejected(self):
        names=self.build_fixture()
        self.put('.copilot/frontend-dist/index.html',b'<script src="/assets/old.js"></script>')
        with patch.object(release,'source_names',return_value=names):
            with self.assertRaisesRegex(ValueError,'index与构建manifest不匹配'):
                release.collect(self.root)

    def test_collect_current_bytes_and_only_manifest_assets(self):
        names=self.build_fixture()
        for name in ('LICENSE','scripts/managed_server.py','deploy/requirements.txt','backend/changed.py'):
            self.put(name,b'working tree change')
            names.append(name)
        for name in release.EXAMPLE_FILES:
            self.put('.copilot/examples/water-saving/'+name)
        with patch.object(release,'source_names',return_value=names):
            entries,rows=release.collect(self.root)
        self.assertNotIn('.copilot/frontend-dist/assets/obsolete.js',entries)
        row=next(r for r in rows if r['path']=='backend/changed.py')
        self.assertEqual(row['sha256'],hashlib.sha256(b'working tree change').hexdigest())
        archive=self.root/'candidate.zip'
        release.write_archive(archive,entries,rows,{'source':'working tree'})
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(z.read('backend/changed.py'),b'working tree change')
            for r in json.loads(z.read('release-manifest.json')):
                self.assertEqual(hashlib.sha256(z.read(r['path'])).hexdigest(),r['sha256'])
        with self.assertRaises(FileExistsError):
            release.write_archive(archive,entries,rows,{})

    def test_exact_secret_value_blocks_packaging(self):
        names=self.build_fixture()
        for name in ('LICENSE','scripts/managed_server.py','deploy/requirements.txt'):
            self.put(name); names.append(name)
        for name in release.EXAMPLE_FILES:
            self.put('.copilot/examples/water-saving/'+name)
        self.put('.env.copilot',b'IMAGE_API_KEY=synthetic-release-secret\n')
        self.put('LICENSE',b'synthetic-release-secret')
        with patch.object(release,'source_names',return_value=names):
            with self.assertRaisesRegex(ValueError,'疑似凭据'):
                release.collect(self.root)

    def test_change_between_scan_and_archive_is_rejected(self):
        path=self.put('a.txt',b'new')
        rows=[{'path':'a.txt','bytes':3,'sha256':hashlib.sha256(b'old').hexdigest()}]
        with self.assertRaisesRegex(ValueError,'文件改变'):
            release.write_archive(self.root/'changed.zip',{'a.txt':path},rows,{})

    def test_secret_example_matches_current_managed_preset(self):
        from dotenv import dotenv_values
        tree=ast.parse((ROOT/'scripts/presentation_copilot.py').read_text(encoding='utf-8'))
        preset=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
                    and any(isinstance(t,ast.Name) and t.id=='PRESET' for t in n.targets))
        example=dotenv_values(ROOT/'.env.copilot.example',interpolate=False)
        for key,value in preset.items():
            self.assertEqual(example[key],value,key)
        self.assertFalse(example['TEXT_API_KEY'])
        self.assertFalse(example['IMAGE_API_KEY'])

    def test_server_template_matches_validator_and_unit_entry(self):
        from services.runtime_config import validate
        raw=json.loads((ROOT/'deploy/runtime.server.example.json').read_text(encoding='utf-8'))
        with self.assertRaisesRegex(ValueError,'域名尚未填写'):
            validate(raw,self.root,source=self.root/'runtime.json')
        raw.update(public_origin='https://slides.example.test',data_dir=str(self.root/'data'),
                   secret_file=str(self.root/'.env.copilot'))
        cfg=validate(raw,self.root,source=self.root/'runtime.json')
        self.assertEqual(cfg['mode'],'preview')
        self.assertIsNone(cfg['authorization'])
        self.assertEqual(cfg['visitor']['default_auto_pages'],5)
        self.assertEqual(cfg['visitor']['pages'],10)
        unit=(ROOT/'deploy/presentation-copilot.service.example').read_text(encoding='utf-8')
        self.assertEqual(unit.count('ExecStart='),1)
        self.assertIn('scripts/presentation_copilot.py serve --runtime-config /etc/presentation-copilot/runtime.json',unit)
        self.assertEqual(unit.count('REPLACE_RELEASE_DIR'),3)

    def test_small_server_limits_do_not_weaken_local_guard(self):
        from services.runtime_config import validate, LOCAL_RESOURCES
        raw=json.loads((ROOT/'deploy/runtime.server.example.json').read_text(encoding='utf-8'))
        raw.update(public_origin='https://slides.example.test',data_dir=str(self.root/'data'),
                   secret_file=str(self.root/'.env.copilot'))
        cfg=validate(raw,self.root,source=self.root/'runtime.json')
        self.assertEqual((cfg['http_threads'],cfg['connection_limit'],cfg['queue']),(4,16,1))
        self.assertEqual(cfg['visitor']['queue'],cfg['queue'])
        self.assertEqual(cfg['visitor']['service_concurrency'],1)
        self.assertEqual(cfg['resources']['start_memory_gib'],0.75)
        self.assertEqual(cfg['resources']['stop_memory_gib'],0.25)
        self.assertEqual(cfg['resources']['rss_mib'],1024)
        unit=(ROOT/'deploy/presentation-copilot.service.example').read_text(encoding='utf-8')
        self.assertIn('MemoryHigh=1100M',unit)
        self.assertIn('MemoryMax=1280M',unit)
        self.assertEqual((LOCAL_RESOURCES['start_memory_gib'],LOCAL_RESOURCES['stop_memory_gib']), (6,4))
        self.assertEqual(LOCAL_RESOURCES['rss_mib'],1536)
        raw.update(profile='local',proxy=False,public_origin='http://127.0.0.1:5011')
        with self.assertRaisesRegex(ValueError,'不能降低本机资源保护'):
            validate(raw,self.root,source=self.root/'runtime.json')


if __name__=='__main__':
    unittest.main()
