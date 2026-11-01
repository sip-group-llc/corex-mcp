import base64
import json
from pathlib import Path
import tempfile
import unittest

import httpx
import jsonschema
from corex_mcp.client import CoreX, CoreXError, UGC_TOOLS, catalog

TOKEN='corex_studio_'+'a'*43
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aP1sAAAAASUVORK5CYII=')


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.media=self.root/'media';self.media.mkdir()
        self.calls=[]
        self.handler=lambda request:httpx.Response(200,json={'jsonrpc':'2.0','id':json.loads(request.content)['id'],
                                                            'result':{'content':[{'type':'text','text':'ok'}],'structuredContent':{'ok':True}}})
    def client(self,**kwargs):
        def call(request):self.calls.append(request);return self.handler(request)
        client=CoreX(credentials=self.root/'private/config.json',base='https://corex.test',token=TOKEN,
                     upload_root=self.media,transport=httpx.MockTransport(call),**kwargs)
        self.addCleanup(client.close);return client

    def test_workspace_catalog_and_scopes(self):
        self.assertEqual(len(catalog()),39)
        self.assertNotIn('get_balance',{x['name'] for x in catalog()})
        self.assertEqual(len(self.client().operations),40)
        client=CoreX(credentials=self.root/'missing',base='https://corex.test',token='corex_ugc_'+'b'*43)
        self.addCleanup(client.close)
        self.assertEqual(set(client.operations),UGC_TOOLS|{'upload_file'})
        for item in catalog():jsonschema.Draft202012Validator.check_schema(item['inputSchema'])

    def test_only_scoped_tokens_and_safe_origins(self):
        for token in ['corex_ag_'+'a'*43,'jwt','corex_studio_a','corex_studio_'+'a'*43+'\r\nX: y']:
            with self.subTest(token=token),self.assertRaises(CoreXError):CoreX(credentials=self.root/'missing',token=token)
        for base in ['http://example.com','https://user:pass@example.com','https://example.com/path','https://example.com?token=secret']:
            with self.subTest(base=base),self.assertRaises(CoreXError):CoreX(credentials=self.root/'missing',token=TOKEN,base=base)

    def test_fixed_endpoint_and_token_not_in_arguments(self):
        result=self.client().call('get_integration_context',{})
        self.assertFalse(result.get('isError',False))
        request=self.calls[0]
        self.assertEqual(str(request.url),'https://corex.test/api/studio/mcp')
        self.assertEqual(request.headers['authorization'],'Bearer '+TOKEN)
        self.assertNotIn(TOKEN,request.content.decode())

    def test_unknown_fields_and_operator_tools_rejected_before_request(self):
        client=self.client()
        for name,args in [('get_balance',{}),('get_integration_context',{'workspaceId':'foreign'}),('upload_file',{'file_path':'x','confirmed':False})]:
            with self.subTest(name=name),self.assertRaises(CoreXError):client.call(name,args)
        self.assertEqual(self.calls,[])

    def test_remote_secret_echo_is_redacted(self):
        def handler(request):
            return httpx.Response(200,json={'jsonrpc':'2.0','id':json.loads(request.content)['id'],
              'result':{'content':[{'type':'text','text':TOKEN}],'structuredContent':{'password':'private','text':TOKEN}}})
        self.handler=handler
        result=json.dumps(self.client().call('get_integration_context',{}))
        self.assertNotIn(TOKEN,result);self.assertNotIn('private',result)

    def test_lost_mutation_reply_never_retries(self):
        def handler(request):raise httpx.ReadTimeout('private URL and token',request=request)
        self.handler=handler
        with self.assertRaises(CoreXError) as error:
            self.client().call('generate_studio',{'toolId':'generate-image','input':{'prompt':'test'},'maxCredits':1,'idempotencyKey':'same'})
        self.assertTrue(error.exception.uncertain);self.assertEqual(len(self.calls),1)
        self.assertNotIn('private URL',str(error.exception))

    def test_redirect_not_followed_and_wrong_response_id_denied(self):
        self.handler=lambda request:httpx.Response(302,headers={'Location':'https://other.test/'})
        with self.assertRaises(CoreXError):self.client().call('get_integration_context',{})
        self.assertEqual(len(self.calls),1)
        self.handler=lambda request:httpx.Response(200,json={'jsonrpc':'2.0','id':'different','result':{}})
        with self.assertRaises(CoreXError):self.client().call('get_integration_context',{})

    def test_confirmed_upload_transmits_bytes_once_and_does_not_register(self):
        file=self.media/'pixel.png';file.write_bytes(PNG)
        def handler(request):
            self.assertEqual(request.url.path,'/api/studio/uploads')
            self.assertIn(PNG,request.content)
            return httpx.Response(201,json={'key':'studio/pixel.png','sizeBytes':len(PNG)})
        self.handler=handler
        result=self.client().call('upload_file',{'file_path':str(file),'confirmed':True})
        data=result['structuredContent'];self.assertFalse(data['registered'])
        self.assertEqual(data['next_tool'],'add_asset');self.assertEqual(data['bytes'],len(PNG))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(list((self.root/'private/upload-cache').iterdir()),[])

    def test_outside_root_hidden_nonmedia_and_bad_content_never_sent(self):
        outside=self.root/'outside.png';outside.write_bytes(PNG)
        files=[outside,self.media/'.secret.png',self.media/'file.py',self.media/'fake.png']
        for file in files[1:]:file.write_bytes(b'not a media file')
        client=self.client()
        for file in files:
            with self.subTest(file=file),self.assertRaises(CoreXError):client.call('upload_file',{'file_path':str(file),'confirmed':True})
        self.assertEqual(self.calls,[])

    def test_upload_size_limit_is_checked_before_transmission(self):
        file=self.media/'pixel.png';file.write_bytes(PNG)
        client=self.client();client.MAX_UPLOAD=1
        with self.assertRaises(CoreXError):client.call('upload_file',{'file_path':str(file),'confirmed':True})
        self.assertEqual(self.calls,[])

    def test_symlink_upload_denied(self):
        source=self.root/'outside.png';source.write_bytes(PNG)
        link=self.media/'link.png'
        try:link.symlink_to(source)
        except OSError:self.skipTest('Symlink creation unavailable for this account')
        with self.assertRaises(CoreXError):self.client().call('upload_file',{'file_path':str(link),'confirmed':True})
        self.assertEqual(self.calls,[])

    def test_incomplete_upload_response_is_uncertain_without_retry(self):
        file=self.media/'pixel.png';file.write_bytes(PNG)
        self.handler=lambda request:httpx.Response(201,json={})
        with self.assertRaises(CoreXError) as error:self.client().call('upload_file',{'file_path':str(file),'confirmed':True})
        self.assertTrue(error.exception.uncertain);self.assertEqual(len(self.calls),1)


if __name__=='__main__':unittest.main()
