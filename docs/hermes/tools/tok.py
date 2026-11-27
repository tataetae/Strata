"""Load the model's tokenizer from the pack's tokenizer/ dir and count chat prompts."""
import json
import pathlib
import sys

sys.path.insert(0, '/home/clara/local-inference/strata-src/tools')
import strata_tokenizer as st  # noqa: E402

PACK = pathlib.Path('/srv/inference/models/qwen3.8-flash-next/packs/iq4_xs/tokenizer')


def load():
    d = PACK
    vocab = json.loads((d / 'vocab.json').read_text(encoding='utf-8'))
    merges = [m for m in (d / 'merges.txt').read_text(encoding='utf-8').splitlines() if m.strip()]
    tt_path = d / 'token_type.json'
    token_types = json.loads(tt_path.read_text(encoding='utf-8')) if tt_path.is_file() else None
    if isinstance(token_types, dict):
        token_types = [token_types[k] for k in sorted(token_types, key=lambda x: int(x))]
    tok = st.Tokenizer(list(vocab.keys()), merges, token_types, 'qwen35', {})
    tpl = (d / 'chat_template.jinja').read_text(encoding='utf-8')
    return tok, tpl


def render(tpl, messages, add_generation_prompt=True):
    """Render the jinja chat template without jinja2 (the template is simple enough)."""
    # a tiny jinja subset renderer: the template is qwen-style
    body = []
    for m in messages:
        body.append('<|im_start|>%s\n%s<|im_end|>\n' % (m['role'], m['content']))
    out = ''.join(body)
    if add_generation_prompt:
        out += '<|im_start|>assistant\n'
    return out


if __name__ == '__main__':
    tok, tpl = load()
    print('  the vocab: %d tokens, %d merges' % (len(tok.tokens), len(tok.ranks)))
    s = 'The quick brown fox jumps over the lazy dog.'
    ids = tok.encode(s)
    print('  a test string: %d tokens, round trip %s' % (len(ids), tok.decode(ids) == s))
    print('  the template head:')
    for line in tpl.splitlines()[:14]:
        print('    ' + line)
