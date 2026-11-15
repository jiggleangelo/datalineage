import http.client,json,tempfile,threading,unittest
from pathlib import Path
from urllib.parse import urlencode,urlsplit
import app
class AppTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.server=app.create_server({'APP_HOST':'127.0.0.1','PORT':'0','PUBLIC_BASE_URL':'http://127.0.0.1:0','DATA_DIR':self.tmp.name})
  self.server.RequestHandlerClass.state=self.server.state if hasattr(self.server.RequestHandlerClass,'state') else None
  self.server.state.netloc='127.0.0.1:'+str(self.server.server_port);self.server.state.base='http://'+self.server.state.netloc
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join(5);self.tmp.cleanup()
 def req(self,method,path,form=None):
  body=urlencode(form or {}).encode() if form is not None else None;headers={'Host':self.server.state.netloc}
  if form is not None:headers.update({'Origin':self.server.state.base,'Content-Type':'application/x-www-form-urlencoded','Content-Length':str(len(body))})
  c=http.client.HTTPConnection('127.0.0.1',self.server.server_port);c.request(method,path,body,headers);r=c.getresponse();data=r.read();out=(r.status,dict(r.getheaders()),data);c.close();return out
 def workspace(self,sample=False):
  status,headers,_=self.req('POST','/w/sample' if sample else '/w',{});self.assertEqual(status,303);return headers['Location']
 def test_health_notice_workspace_card_download_export_delete(self):
  self.assertEqual(self.req('GET','/healthz')[:1],(200,));self.assertIn(app.NOTICE.encode(),self.req('GET','/')[2]);w=self.workspace();self.assertIn(b'This link is the only key',self.req('GET',w)[2]);card='---\nlicense: mit\n---\nLicence: non-commercial use only.';status,headers,_=self.req('POST',w+'/card',{'name':'local','card':card});self.assertEqual(status,303);r=headers['Location'];page=self.req('GET',r)[2];self.assertIn(b'DECLARED_DETECTED_MISMATCH',page);self.assertIn(b'Rule-based extraction',page)
  for name in ('report.json','bom.json','report.pdf','template.json','template.pdf'):
   status,_,body=self.req('GET',r+'/'+name);self.assertEqual(status,200);self.assertTrue(body.startswith(b'%PDF') if name.endswith('.pdf') else bool(json.loads(body)))
  token=w.split('/')[-1];disk=b''.join(p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file());self.assertNotIn(token.encode(),disk);self.assertNotIn(card.encode(),disk)
  status,_,_=self.req('POST',w+'/delete',{'confirm':'yes'});self.assertEqual(status,200);self.assertEqual(self.req('GET',w)[0],404)
 def test_guards_sample_and_persistence(self):
  w=self.workspace(True);self.assertIn(b'made-up data',self.req('GET',w)[2]);c=http.client.HTTPConnection('127.0.0.1',self.server.server_port);c.request('GET','/',headers={'Host':'evil.example'});self.assertEqual(c.getresponse().status,403);c.close();self.assertEqual(self.req('POST',w+'/share',{})[0],200)
if __name__=='__main__':unittest.main()
