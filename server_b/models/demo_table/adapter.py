"""Deterministic domain-neutral demo. No trained weights or scientific claims."""
import csv
import math


class Adapter:
    def metadata(self):return {'mode':'demo','semantics':'sum(selected numeric columns) * variant scale + bias'}
    def validate(self,deployment,inputs,parameters):
        columns=parameters.get('columns',[])
        if not columns or len(set(columns))!=len(columns):raise ValueError('Select unique numeric CSV columns')
    def load(self,deployment,variant,device):
        if device!='cpu':raise ValueError('Demo declares CPU execution only')
        self.variant=variant['configuration']
    def preprocess(self,inputs,parameters):return inputs['dataset']['path'],parameters['columns']
    def predict(self,prepared,progress,cancellation,output_dir):
        path,columns=prepared;preview=[];count=0
        with open(path,encoding='utf-8-sig',newline='') as source,(output_dir/'predictions.csv').open('w',encoding='utf-8',newline='') as target:
            reader=csv.DictReader(source)
            if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames) or any(c not in reader.fieldnames for c in columns):raise ValueError('Missing or duplicate CSV column')
            writer=csv.writer(target);writer.writerow(['row_index','prediction'])
            for index,row in enumerate(reader):
                if cancellation():raise ValueError('Task cancelled')
                if index>=1000000:raise ValueError('Demo row limit reached')
                values=[float(row[name]) for name in columns]
                if not all(math.isfinite(value) for value in values):raise ValueError('Non-finite input; no silent imputation')
                value=sum(values)*self.variant['scale']+self.variant['bias']
                if not math.isfinite(value):raise ValueError('Non-finite prediction')
                writer.writerow([index,value]);count+=1
                if len(preview)<128:preview.append({'row_index':index,'prediction':value})
                if count%1000==0:progress(count)
        return {'mode':'demo','metrics':{'processed_rows':count},'preview':preview,'artifacts':[{'path':'predictions.csv','kind':'table','format':'csv'}],
                'warnings':['Communication demonstration only; not a trained scientific model']}
    def postprocess(self,prediction):return prediction
    def self_test(self,variant):
        expected=3*variant['configuration']['scale']+variant['configuration']['bias']
        return {'ok':expected in {3,7},'fixture_input':[1,2],'actual_output':expected,'mode':'demo'}


def create_adapter():return Adapter()
