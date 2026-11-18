import pytest

from app.core.errors import ProviderAddressRefused
from app.services.providers import ssrf
from app.services.providers.catalog import (
    CHAT_PROVIDERS,
    EMBEDDING_PROVIDERS,
    allowed_hosts,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "::1",
        "::ffff:127.0.0.1",  # IPv4 loopback written as IPv6
        "::ffff:10.0.0.1",
        "fc00::1",  # unique local
        "fe80::1",  # link local
        "224.0.0.1",  # multicast
        "not an address",
        "",
    ],
)
def test_internal_and_malformed_addresses_are_not_public(address: str) -> None:
    assert ssrf.is_public_ip(address) is False


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_public(address: str) -> None:
    assert ssrf.is_public_ip(address) is True


def test_every_catalogue_address_passes_when_it_resolves_publicly() -> None:
    urls = [p.base_url for p in CHAT_PROVIDERS.values()]
    urls += [p.base_url for p in EMBEDDING_PROVIDERS.values()]
    for url in urls:
        ssrf.check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",  # not https
        "https://evil.example/v1",  # not in the catalogue
        "https://api.openai.com.evil.example/v1",  # look-alike host
        "https://api.openai.com@evil.example/v1",  # userinfo trick: the host is evil.example
        "https://user:pw@api.openai.com/v1",  # credentials
        "https://api.openai.com:8443/v1",  # odd port
        "https://127.0.0.1/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/v1",
        "https://localhost/v1",
        "file:///etc/passwd",
        "ftp://api.openai.com/v1",
        "",
        "https:///v1",
    ],
)
def test_addresses_outside_the_catalogue_are_refused(url: str) -> None:
    with pytest.raises(ProviderAddressRefused):
        ssrf.check_url(url)


def test_a_catalogue_host_is_refused_if_it_resolves_to_an_internal_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:192.168.0.1"):
        monkeypatch.setattr(ssrf, "resolver", lambda host, bad=bad: [bad])
        with pytest.raises(ProviderAddressRefused):
            ssrf.check_url("https://api.openai.com/v1")


def test_one_internal_answer_among_public_ones_is_enough_to_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssrf, "resolver", lambda host: ["93.184.216.34", "10.0.0.1"])
    with pytest.raises(ProviderAddressRefused):
        ssrf.check_url("https://api.openai.com/v1")


def test_a_failed_or_empty_lookup_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(host: str) -> list[str]:
        raise OSError("no dns")

    monkeypatch.setattr(ssrf, "resolver", boom)
    with pytest.raises(ProviderAddressRefused):
        ssrf.check_url("https://api.openai.com/v1")
    monkeypatch.setattr(ssrf, "resolver", lambda host: [])
    with pytest.raises(ProviderAddressRefused):
        ssrf.check_url("https://api.openai.com/v1")


def test_the_host_list_is_exactly_the_catalogue() -> None:
    assert allowed_hosts() == {
        "openrouter.ai",
        "api.openai.com",
        "api.together.xyz",
        "api.groq.com",
        "api.cloudflare.com",
    }
