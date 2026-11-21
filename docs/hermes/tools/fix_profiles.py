"""Make every profile local except codex-worker (the owner's stated exception)."""
import sys, shutil
sys.path.insert(0, '/home/clara/.hermes/hermes-agent')
from ruamel.yaml import YAML
LIVE='qwen3.8-flash-next-iq4_xs'; URL='http://127.0.0.1:8081/v1'
def _key() -> str:
    import yaml
    cfg = yaml.safe_load(open('/home/clara/.hermes/config.yaml')) or {}
    return ((cfg.get('providers') or {}).get('strata') or {}).get('api_key') or ''
y=YAML(); y.preserve_quotes=True

def set_model(d, model=LIVE, provider='strata', base_url=URL, api_mode='chat_completions'):
    m=d.get('model') or {}
    m['default']=model; m['provider']=provider; m['base_url']=base_url
    m['api_mode']=api_mode; m['context_length']=262144
    d['model']=m

# codexworker: ollama-cloud -> the local engine (it is dormant; the name stays for the roster)
p='/home/clara/.hermes/profiles/codexworker/config.yaml'
shutil.copy2(p, p+'.bak-20261121T0420-local')
with open(p) as f: d=y.load(f)
set_model(d)
aux=d.get('auxiliary') or {}
aux.setdefault('compression',{})
aux['compression'].update({'provider':'strata','model':LIVE,'base_url':URL,'api_key':_key(),
                            'context_length':262144,'timeout':1800,'reasoning_effort':'none'})
d['auxiliary']=aux
with open(p,'w') as f: y.dump(d,f)
print("  codexworker   -> %s / strata (was deepseek-v4.1-flash:1017 / ollama-cloud)" % LIVE)

# advisor: its default is the codex runtime; the owner's exception is 'codex worker'.
# advisor is a separate profile and is dormant -> local, so no profile but codex-worker talks to a cloud.
p='/home/clara/.hermes/profiles/advisor/config.yaml'
shutil.copy2(p, p+'.bak-20261121T0420-local')
with open(p) as f: d=y.load(f)
set_model(d)
aux=d.get('auxiliary') or {}
aux.setdefault('compression',{})
aux['compression'].update({'provider':'strata','model':LIVE,'base_url':URL,'api_key':_key(),
                            'context_length':262144,'timeout':1800,'reasoning_effort':'none'})
if isinstance(aux.get('vision'), dict):
    aux['vision'].update({'provider':'strata','model':LIVE,'base_url':URL,'api_key':KEY})
d['auxiliary']=aux
with open(p,'w') as f: y.dump(d,f)
print("  advisor       -> %s / strata (was gpt-6.1-sol / openai-codex)" % LIVE)
print("  codex-worker  -> gpt-6.1-sol / openai-codex  (KEPT: the owner's exception)")
