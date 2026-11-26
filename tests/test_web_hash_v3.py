import hashlib
from pathlib import Path
import shutil
import subprocess
import unittest


class BrowserHashTests(unittest.TestCase):
    def test_incremental_hash_matches_python_at_padding_boundaries(self):
        node=shutil.which('node')
        if not node:self.skipTest('Existing Node is unavailable')
        script=Path(__file__).resolve().parents[1]/'web/sha256.js'
        for length in [0,1,55,56,63,64,65,16384,65537]:
            source="const SHA=require(process.argv[1]);const n=Number(process.argv[2]);const b=Uint8Array.from({length:n},(_,i)=>i%251);const h=new SHA();for(let i=0;i<n;i+=37)h.update(b.subarray(i,i+37));console.log(h.hex());"
            actual=subprocess.check_output([node,'-e',source,str(script),str(length)],text=True).strip()
            self.assertEqual(actual,hashlib.sha256(bytes(i%251 for i in range(length))).hexdigest())


if __name__=='__main__':unittest.main()
