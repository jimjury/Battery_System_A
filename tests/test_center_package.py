import importlib.util
import tempfile
from pathlib import Path
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('center_installer',ROOT/'deploy/install_center.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


class CenterBundleTests(unittest.TestCase):
    def test_source_bundle_integrity_and_private_exclusions(self):
        import sys
        sys.path.insert(0,str(ROOT/'tools'))
        from package_center import package
        result=package('unit-test-source-bundle');path=Path(result['path'])
        version,files=installer.verify_bundle(path,result['sha256'])
        self.assertEqual(version,'unit-test-source-bundle');self.assertIn('control_center/ui/index.html',files)
        self.assertFalse(any('.runtime' in name or '.pem' in name or '.key' in name for name in files))
        with self.assertRaises(ValueError):installer.verify_bundle(path,'0'*64)


if __name__=='__main__':unittest.main()
