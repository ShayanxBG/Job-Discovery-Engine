#!/usr/bin/env python3
"""Deterministic gate on whether an EXTERNAL target is safe to fetch or navigate.

WHAT THIS DEFENDS AGAINST. Every URL this workspace follows ultimately comes from
untrusted content: a search result, a board listing, an `apply` link inside a job
advert, a redirect. A hostile or merely broken page can offer a link that points
somewhere the agent should never go, and the classic shapes are well known:

    file:///C:/Users/.../.ssh/id_rsa      read a local file
    http://127.0.0.1:8080/admin           reach a service on this machine
    http://192.168.1.1/                   reach something on the local network
    javascript:...                        execute in a browser context
    data:text/html;base64,...             smuggle a page with no origin

None of those is a job advert. They are all trivially recognisable BEFORE any
request is made, so they are refused at the input boundary rather than relied on
to fail safely later.

WHAT THIS IS NOT. It is not network security, it is not a sandbox, and it does not
replace whatever the browser or the fetch tool enforces. A determined attacker
controls DNS, and a hostname that resolves to a private address today may not have
yesterday: this module cannot see that and does not pretend to. It is a cheap
deterministic gate that removes the obvious, and its value is that it is applied
uniformly at every place an external URL enters the workflow.

REDIRECTS. A safe URL can redirect to an unsafe one, so the FINAL target must be
judged too wherever the tool surface exposes it. `classify` takes an optional
`final_url`, and a chain that ends somewhere unsafe is unsafe however it started.

CANONICALISATION IS NOT OWNED HERE. `job_state.norm_url` owns vacancy identity.
This module only decides whether a target may be visited, and imports that
canonicaliser rather than growing a second, subtly different one.
"""
import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_state import norm_url  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

VERDICTS = ('safe', 'unsafe', 'unknown')

# https only, in practice. http is tolerated because a small number of legitimate
# UK boards still serve it, but it is reported so a caller can prefer the secure
# form and never silently downgrade.
ALLOWED_SCHEMES = ('https', 'http')
PREFERRED_SCHEME = 'https'

# Every one of these can reach something that is not a job advert. `javascript:`
# and `data:` execute or fabricate content; `file:` reads the disk; the rest reach
# browser internals or non-web services.
FORBIDDEN_SCHEMES = (
    'file', 'data', 'javascript', 'vbscript', 'ftp', 'ftps', 'sftp', 'ssh', 'telnet',
    'gopher', 'ldap', 'ldaps', 'dict', 'tftp', 'smb', 'nfs', 'mailto', 'tel', 'sms',
    'chrome', 'chrome-extension', 'chrome-search', 'about', 'blob', 'view-source',
    'resource', 'moz-extension', 'edge', 'ms-appx', 'intent', 'jar', 'content',
)

# Hostnames that always mean this machine.
LOCAL_HOSTNAMES = ('localhost', 'localhost.localdomain', 'ip6-localhost', 'ip6-loopback')
# Suffixes that resolve on the LOCAL NETWORK rather than the public internet. These
# are the security case: a page steering a fetch at one of these is aiming at
# something inside the perimeter.
LOCAL_SUFFIXES = ('.local', '.localhost', '.localdomain', '.internal', '.intranet',
                  '.lan', '.home', '.corp', '.private')
# Suffixes RFC 2606 and RFC 6761 reserve so they never resolve anywhere. They are
# not a local-network risk, but they are never a real vacancy either, so they are
# refused with an accurate reason rather than being mislabelled as local.
RESERVED_SUFFIXES = ('.test', '.example', '.invalid')

REASONS = (
    'ok', 'empty_target', 'malformed', 'forbidden_scheme', 'missing_scheme',
    'relative_url', 'no_host', 'local_hostname', 'local_suffix', 'loopback_address',
    'private_address', 'link_local_address', 'unspecified_address', 'reserved_address',
    'reserved_suffix',
    'credentials_in_url', 'unsafe_redirect_target', 'insecure_scheme',
)


def safety_error(message, *hints):
    lines = [message]
    lines.extend(f'  {hint}' for hint in hints)
    return SystemExit('\n'.join(lines))


def _strip_brackets(host):
    return host[1:-1] if host.startswith('[') and host.endswith(']') else host


def _address_problem(host):
    """The reason a host is a local or otherwise non-public address, or ''.

    Only a host that is ALREADY a literal address is judged here. A hostname is
    not resolved: doing so would turn an input check into a network call, and the
    answer could differ by the time anything actually fetched it.
    """
    try:
        address = ipaddress.ip_address(_strip_brackets(host))
    except ValueError:
        return ''
    if address.is_loopback:
        return 'loopback_address'
    if address.is_link_local:
        return 'link_local_address'
    if address.is_unspecified:
        return 'unspecified_address'
    if address.is_private:
        return 'private_address'
    if address.is_reserved or address.is_multicast:
        return 'reserved_address'
    return ''


def classify(url, final_url='', allow_http=True):
    """Whether one external target may be fetched or navigated to.

    Returns a dict carrying the verdict, a controlled reason, and enough detail to
    report the refusal honestly. A URL is judged as given; when `final_url` is
    supplied the chain is only safe if BOTH ends are safe.
    """
    raw = str(url or '').strip()
    result = {
        'url': raw,
        'verdict': 'unsafe',
        'reason': 'empty_target',
        'scheme': '',
        'host': '',
        'secure': False,
        'canonical_url': '',
        'warnings': [],
        'detail': '',
    }
    if not raw:
        result['detail'] = 'No target was supplied.'
        return result

    # A control character or whitespace inside a URL is a smuggling attempt or a
    # copy/paste accident, never a real link.
    if re.search(r'[\x00-\x20\x7f]', raw):
        result.update({'reason': 'malformed',
                       'detail': 'The target contains whitespace or control characters.'})
        return result

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        result.update({'reason': 'malformed', 'detail': f'{type(exc).__name__}: {exc}'})
        return result

    scheme = (parts.scheme or '').lower()
    result['scheme'] = scheme
    if not scheme:
        result.update({'reason': 'relative_url' if not raw.startswith('//') else 'missing_scheme',
                       'detail': 'An external target must be an absolute https URL. A '
                                 'relative or scheme-relative link from a page is not a '
                                 'destination this workspace resolves.'})
        return result
    if scheme in FORBIDDEN_SCHEMES:
        result.update({'reason': 'forbidden_scheme',
                       'detail': f'The {scheme}: scheme cannot address a job advert. It '
                                 'addresses the local machine, a browser internal, or an '
                                 'executable/inline context.'})
        return result
    if scheme not in ALLOWED_SCHEMES:
        result.update({'reason': 'forbidden_scheme',
                       'detail': f'Unrecognised scheme {scheme}:. Only https (and http '
                                 'where a legitimate source still requires it) are used.'})
        return result

    host = (parts.hostname or '').lower().strip('.')
    result['host'] = host
    if not host:
        result.update({'reason': 'no_host', 'detail': 'The target names no host.'})
        return result

    # Credentials embedded in a URL are how a link disguises where it really goes.
    if parts.username or parts.password:
        result.update({'reason': 'credentials_in_url',
                       'detail': 'The target embeds credentials, which also disguises the '
                                 'real host from a human reading the link.'})
        return result

    if host in LOCAL_HOSTNAMES:
        result.update({'reason': 'local_hostname',
                       'detail': f'{host} is this machine, not a job board.'})
        return result
    for suffix in LOCAL_SUFFIXES:
        if host.endswith(suffix):
            result.update({'reason': 'local_suffix',
                           'detail': f'{host} resolves on the local network, not the '
                                     'public internet. An untrusted page must never '
                                     'steer a fetch inside the perimeter.'})
            return result
    for suffix in RESERVED_SUFFIXES:
        if host.endswith(suffix):
            result.update({'reason': 'reserved_suffix',
                           'detail': f'{host} uses a reserved name that never resolves '
                                     '(RFC 2606/6761). It cannot be a real vacancy.'})
            return result
    address_problem = _address_problem(host)
    if address_problem:
        result.update({'reason': address_problem,
                       'detail': f'{host} is a local, private, link-local or reserved '
                                 'address. An untrusted page must never steer a fetch '
                                 'towards a service on this machine or network.'})
        return result

    result['secure'] = scheme == 'https'
    if scheme == 'http':
        if not allow_http:
            result.update({'reason': 'insecure_scheme',
                           'detail': 'http was refused because this caller requires https.'})
            return result
        result['warnings'].append(
            'Target is plain http. Prefer the https form when the source offers one; '
            'the content is readable and alterable in transit.')

    # A chain is only as safe as where it ends.
    if final_url and norm_url(final_url) != norm_url(raw):
        final = classify(final_url, allow_http=allow_http)
        if final['verdict'] != 'safe':
            result.update({
                'reason': 'unsafe_redirect_target',
                'final_url': final['url'],
                'final_reason': final['reason'],
                'detail': f"The target redirects to {final['url']}, which is unsafe: "
                          f"{final['detail']}",
            })
            return result
        result['final_url'] = final['url']
        result['warnings'].extend(final['warnings'])

    result.update({'verdict': 'safe', 'reason': 'ok', 'canonical_url': norm_url(raw),
                   'detail': ''})
    return result


def is_safe(url, final_url='', allow_http=True):
    return classify(url, final_url=final_url, allow_http=allow_http)['verdict'] == 'safe'


def check_batch(rows, allow_http=True):
    """Classify many targets in one process, as the other batch gates do."""
    results = []
    for index, row in enumerate(rows or []):
        row = row if isinstance(row, dict) else {'url': row}
        verdict = classify(row.get('url', ''), final_url=row.get('final_url', ''),
                           allow_http=allow_http)
        results.append({'index': index, **verdict})
    return {
        'count': len(results),
        'safe_count': sum(1 for r in results if r['verdict'] == 'safe'),
        'unsafe_count': sum(1 for r in results if r['verdict'] != 'safe'),
        'unsafe_reasons': sorted({r['reason'] for r in results if r['verdict'] != 'safe'}),
        'results': results,
        'note': 'A deterministic input gate, not network security. It removes obviously '
                'unsafe targets before a request is made; it does not resolve hostnames '
                'and cannot see where DNS points at fetch time.',
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def read_json_input(args):
    if getattr(args, 'file', ''):
        path = Path(args.file)
        if not path.exists():
            raise safety_error(f'Input file not found: {path}')
        raw = path.read_text(encoding='utf-8')
    else:
        raw = sys.stdin.read()
    raw = raw.lstrip('\ufeff')
    if not raw.strip():
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise safety_error('Malformed JSON input.',
                           f'JSON error at line {exc.lineno} column {exc.colno}: {exc.msg}') from None


def cmd_check(args):
    verdict = classify(args.url, final_url=args.final_url, allow_http=not args.https_only)
    print(json.dumps(verdict, indent=2, ensure_ascii=False))
    raise SystemExit(0 if verdict['verdict'] == 'safe' else 1)


def cmd_check_batch(args):
    rows = read_json_input(args)
    if not isinstance(rows, list):
        raise safety_error('check-batch expects a JSON array of URLs or {url, final_url} rows.')
    report = check_batch(rows, allow_http=not args.https_only)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(0 if report['unsafe_count'] == 0 else 1)


def cmd_policy(args):
    print(json.dumps({
        'allowed_schemes': list(ALLOWED_SCHEMES),
        'preferred_scheme': PREFERRED_SCHEME,
        'forbidden_schemes': list(FORBIDDEN_SCHEMES),
        'local_hostnames': list(LOCAL_HOSTNAMES),
        'local_suffixes': list(LOCAL_SUFFIXES),
        'reserved_suffixes': list(RESERVED_SUFFIXES),
        'refused_address_classes': ['loopback', 'private', 'link_local', 'unspecified',
                                    'reserved', 'multicast'],
        'verdicts': list(VERDICTS),
        'reasons': list(REASONS),
        'note': 'A deterministic input gate applied wherever an external URL enters the '
                'workflow. It is not network security and does not resolve hostnames.',
    }, indent=2, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description='Deterministic external-URL safety gate')
    sub = p.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('check', help='Classify one external target.')
    c.add_argument('url')
    c.add_argument('--final-url', dest='final_url', default='',
                   help='The destination a redirect actually reached, when known.')
    c.add_argument('--https-only', dest='https_only', action='store_true')
    c.set_defaults(func=cmd_check)

    b = sub.add_parser('check-batch', help='Classify many targets in one process.')
    b.add_argument('--file', default='')
    b.add_argument('--https-only', dest='https_only', action='store_true')
    b.set_defaults(func=cmd_check_batch)

    pol = sub.add_parser('policy', help='Show the schemes and address classes refused.')
    pol.set_defaults(func=cmd_policy)

    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
