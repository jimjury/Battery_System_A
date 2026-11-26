'use strict';
class CSVParser {
  constructor(delimiter=','){this.delimiter=delimiter;this.row=[];this.field='';this.quoted=false;this.pending=false;this.closed=false;this.skipLF=false;this.line=0;}
  feed(text,final=false){const rows=[];const field=()=>{this.row.push(this.field);this.field='';this.closed=false;if(this.row.length>256)throw Error('CSV 列数超过 256');};const row=()=>{field();rows.push(this.row);this.row=[];this.line++;};
    for(let i=0;i<text.length;i++){const ch=text[i];if(this.skipLF){this.skipLF=false;if(ch==='\n')continue;}
      if(this.quoted){if(this.pending){if(ch==='"'){this.field+='"';this.pending=false;continue;}this.pending=false;this.quoted=false;this.closed=true;}else{if(ch==='"')this.pending=true;else this.field+=ch;if(this.field.length>4096)throw Error('CSV 单字段超过 4096 字符');continue;}}
      if(ch===this.delimiter){field();continue;}if(ch==='\r'||ch==='\n'){row();this.skipLF=ch==='\r';continue;}
      if(this.closed){if(ch===' '||ch==='\t')continue;throw Error('CSV 引号结束后有非法字符');}
      if(ch==='"'){if(this.field.length)throw Error('CSV 字段中的引号不合法');this.quoted=true;continue;}this.field+=ch;if(this.field.length>4096)throw Error('CSV 单字段超过 4096 字符');
    }
    if(final){if(this.quoted&&!this.pending)throw Error('CSV 引号未闭合');if(this.pending)this.closed=true;this.quoted=false;this.pending=false;if(this.field.length||this.row.length||this.closed)row();}
    return rows;
  }
}
async function* csvRows(file,delimiter=',',hash=null){const parser=new CSVParser(delimiter),decoder=new TextDecoder('utf-8',{fatal:true});for(let offset=0;offset<file.size;offset+=65536){const bytes=new Uint8Array(await file.slice(offset,offset+65536).arrayBuffer());if(hash)hash.update(bytes);for(const row of parser.feed(decoder.decode(bytes,{stream:true})))yield row;}for(const row of parser.feed(decoder.decode(),true))yield row;}
function number(value,line,name){const text=String(value??'').trim();if(!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(text))throw Error(`第 ${line} 行 ${name} 不是有限十进制数`);const n=Number(text);if(!Number.isFinite(n))throw Error(`第 ${line} 行 ${name} 非有限数`);return n;}
async function* mappedRows(file,mapping,units,delimiter=',',hash=null){let header=null,seq=0,line=0,previous=null;for await(const row of csvRows(file,delimiter,hash)){line++;if(!header){header=row.map(x=>x.trim());if(new Set(header).size!==header.length||header.some(x=>!x))throw Error('表头为空或重复，请修正文件');continue;}if(row.length!==header.length)throw Error(`第 ${line} 行列数与表头不一致`);
  const read=(key)=>{const index=mapping[key];if(!Number.isInteger(index)||index<0||index>=row.length)throw Error(`缺少 ${key} 列映射`);return number(row[index],line,key);};
  const sample={seq,sample_time_s:read('time')*(units.time==='ms'?0.001:1),voltage_v:read('voltage')*(units.voltage==='mV'?0.001:1),current_a:read('current')*(units.current==='mA'?0.001:1)*(units.sign==='positive_charge'?-1:1),temperature_c:read('temperature')-(units.temperature==='K'?273.15:0)};
  if(sample.sample_time_s<0||(previous!==null&&sample.sample_time_s<=previous))throw Error(`第 ${line} 行时间必须非负且严格递增`);if(sample.voltage_v<0||sample.voltage_v>6||sample.temperature_c< -273.15||sample.temperature_c>500)throw Error(`第 ${line} 行单位或电芯范围不正确`);
  previous=sample.sample_time_s;let reference=null;if(Number.isInteger(mapping.reference)&&mapping.reference>=0){reference=number(row[mapping.reference],line,'reference SOC')*(units.reference==='fraction'?100:1);if(reference<0||reference>100)throw Error(`第 ${line} 行参考 SOC 不在 0–100%`);}yield {sample,reference_soc:reference};seq++;
}if(seq===0)throw Error('CSV 没有数据行');}
async function inspectCSV(file,mapping,units,delimiter=',',onProgress=()=>{}){const hash=new BatterySHA256();let count=0,period=null,last=null;let preview=[],stride=1;for await(const row of mappedRows(file,mapping,units,delimiter,hash)){if(last!==null){const delta=row.sample.sample_time_s-last;if(period===null)period=delta;else if(Math.abs(delta-period)>Math.max(1e-6,period*1e-6))throw Error(`seq ${count} 采样周期不一致；首版拒绝静默补点`);}last=row.sample.sample_time_s;if(count%stride===0)preview.push(row);if(preview.length>1024){preview=preview.filter((_,i)=>i%2===0);stride*=2;}count++;if(count%2048===0)onProgress(count);}onProgress(count);return {count,period,sha256:hash.hex(),preview};}
const BatteryCSV={CSVParser,csvRows,mappedRows,inspectCSV};globalThis.BatteryCSV=BatteryCSV;if(typeof module!=='undefined')module.exports=BatteryCSV;
