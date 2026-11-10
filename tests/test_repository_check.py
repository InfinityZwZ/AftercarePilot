def test_repository_scanner_detects_secrets_without_echoing_them():
    from scripts.check_repository import inspect_text
    secret='unit-known-'+('x'*32)
    result=inspect_text('payload '+secret+' '+('sk-'+'a'*28),[secret])
    assert 'known_secret' in result and 'api_key_pattern' in result
    assert all(secret not in label for label in result)


def test_repository_scanner_flags_private_paths_and_private_key():
    from scripts.check_repository import inspect_text
    assert 'private_path' in inspect_text('C:/'+'Users/someone/Documents/data',[])
    assert 'private_key' in inspect_text('-----BEGIN '+'PRIVATE KEY-----',[])


def test_repository_links_ignore_remote_and_anchor_and_check_local(tmp_path):
    from scripts.check_repository import broken_links
    (tmp_path/'README.md').write_text('[a](https://example.org) [b](#part) [c](docs/no.md)',encoding='utf-8')
    assert broken_links(tmp_path,'README.md',{'README.md'})==['docs/no.md']
