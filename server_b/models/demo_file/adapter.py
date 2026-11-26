import codecs
import hashlib
import json


class Adapter:
    def metadata(self):return {'mode':'demo','semantics':'Actual byte and UTF-8 text statistics; no model inference claim'}
    def validate(self,deployment,inputs,parameters):pass
    def load(self,deployment,variant,device):
        if device!='cpu':raise ValueError('This plugin is CPU-only')
    def preprocess(self,inputs,parameters):return inputs['document']['path']
    def predict(self,path,progress,cancellation,output_dir):
        decoder=codecs.getincrementaldecoder('utf-8')('strict');size=0;chars=0;words=0;newlines=0;in_word=False;text_valid=True;nonempty=False;last='';sha=hashlib.sha256()
        with open(path,'rb') as source:
            while True:
                data=source.read(16384)
                if not data:break
                if cancellation():raise ValueError('Task cancelled')
                size+=len(data);sha.update(data)
                if text_valid:
                    try:text=decoder.decode(data)
                    except UnicodeDecodeError:text_valid=False;text=''
                    for char in text:
                        chars+=1;nonempty=True;last=char
                        if char=='\n':newlines+=1
                        if char.isspace():in_word=False
                        elif not in_word:words+=1;in_word=True
                progress(size)
        if text_valid:
            try:decoder.decode(b'',final=True)
            except UnicodeDecodeError:text_valid=False
        values={'bytes':size,'sha256':sha.hexdigest(),'utf8_valid':text_valid,'characters':chars if text_valid else None,
                'words':words if text_valid else None,'lines':newlines+(1 if nonempty and last!='\n' else 0) if text_valid else None}
        (output_dir/'statistics.json').write_text(json.dumps(values,sort_keys=True),encoding='utf-8')
        return {'mode':'demo','metrics':values,'preview':[values],'artifacts':[{'path':'statistics.json','kind':'json','format':'json'}],
                'warnings':[] if text_valid else ['Binary file: text counts are unavailable']}
    def postprocess(self,prediction):return prediction
    def self_test(self,variant):return {'ok':len('sample'.encode())==6,'fixture':'sample','actual_bytes':6,'mode':'demo'}


def create_adapter():return Adapter()
