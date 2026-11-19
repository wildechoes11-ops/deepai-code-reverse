import json, os, urllib.request
base=os.environ.get('DEEPAI_PROXY_URL','http://127.0.0.1:8788')
# No hardcoded credential: the proxy accepts any bearer unless
# DEEPAI_GATEWAY_TOKEN is set, in which case export the same value here.
key=os.environ.get('DEEPAI_GATEWAY_TOKEN','local-dev-token')
def call(prompt):
    body={'model':'deepseek-v4.1-flash','prompt':prompt,'max_tokens':64}
    req=urllib.request.Request(base+'/v1/completions',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+key,'X-Conversation-Id':'test1'})
    with urllib.request.urlopen(req,timeout=60) as r:
        return json.loads(r.read())['choices'][0]['text']
print('1:', call('Remember the number 42.'))
print('2:', call('What number did I ask you to remember?'))
