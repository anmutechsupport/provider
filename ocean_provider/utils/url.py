#
# Copyright 2021 Ocean Protocol Foundation
# SPDX-License-Identifier: Apache-2.0
#
import hashlib
import ipaddress
import json
import logging
import os
from urllib.parse import urlparse, urljoin

import dns.resolver
import requests
from ocean_provider.utils.basics import get_config, get_provider_wallet

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 3
CHUNK_SIZE = 8192


def get_redirect(url, redirect_count=0):
    if not is_url(url):
        return None

    if redirect_count > 5:
        logger.info(f"More than 5 redirects for url {url}. Aborting.")

        return None

    result = requests.head(url, allow_redirects=False)

    if result.status_code == 405:
        # HEAD not allowed, so defaulting to get
        result = requests.get(url, allow_redirects=False)

    if result.is_redirect:
        location = urljoin(
            url if url.endswith("/") else f"{url}/", result.headers["Location"]
        )
        logger.info(f"Redirecting for url {url} to location {location}.")

        return get_redirect(location, redirect_count + 1)

    return url


def is_safe_url(url):
    url = get_redirect(url)

    if not url:
        return False

    result = urlparse(url)

    return is_safe_domain(result.hostname)


def is_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:  # noqa
        return False


def is_ip(address):
    return address.replace(".", "").isnumeric()


def is_this_same_provider(url):
    result = urlparse(url)
    try:
        provider_info = requests.get(f"{result.scheme}://{result.netloc}/").json()
        address = provider_info["providerAddress"]
    except (requests.exceptions.RequestException, KeyError):
        address = None

    return address and address.lower() == get_provider_wallet().address.lower()


def _get_records(domain, record_type):
    DNS_RESOLVER = dns.resolver.Resolver()
    try:
        return DNS_RESOLVER.resolve(domain, record_type, search=True)
    except Exception as e:
        logger.info(f"[i] Cannot get {record_type} record for domain {domain}: {e}\n")

        return None


def is_safe_domain(domain):
    ip_v4_records = _get_records(domain, "A")
    ip_v6_records = _get_records(domain, "AAAA")

    result = validate_dns_records(domain, ip_v4_records, "A") and validate_dns_records(
        domain, ip_v6_records, "AAAA"
    )

    if not is_ip(domain):
        return result

    return result and validate_dns_record(domain, domain, "")


def validate_dns_records(domain, records, record_type):
    """
    Verify if all DNS records resolve to public IP addresses.
    Return True if they do, False if any error has been detected.
    """
    if records is None:
        return True

    for record in records:
        if not validate_dns_record(record, domain, record_type):
            return False

    return True


def validate_dns_record(record, domain, record_type):
    value = record if isinstance(record, str) else record.to_text().strip()
    allow_non_public_ip = get_config().allow_non_public_ip

    try:
        ip = ipaddress.ip_address(value)
        # noqa See https://docs.python.org/3/library/ipaddress.html#ipaddress.IPv4Address.is_global
        if ip.is_private or ip.is_reserved or ip.is_loopback:
            if allow_non_public_ip:
                logger.warning(
                    f"[!] DNS record type {record_type} for domain name "
                    f"{domain} resolves to a non public IP address {value}, "
                    "but allowed by config!"
                )
                return True
            else:
                logger.error(
                    f"[!] DNS record type {record_type} for domain name "
                    f"{domain} resolves to a non public IP address {value}. "
                )

                return False
    except ValueError:
        logger.info("[!] '%s' is not valid IP address!" % value)
        return False

    return True


def get_download_url(url_object):
    if url_object["type"] != "ipfs":
        return url_object["url"]

    if not os.getenv("IPFS_GATEWAY"):
        raise Exception("No IPFS_GATEWAY defined, can not resolve ipfs hash.")

    return urljoin(os.getenv("IPFS_GATEWAY"), urljoin("ipfs/", url_object["hash"]))


def check_url_details(url_object, with_checksum=False):
    """
    If the url argument is invalid, returns False and empty dictionary.
    Otherwise it returns True and a dictionary containing contentType and
    contentLength. If the with_checksum flag is set to True, it also returns
    the file checksum and the checksumType (currently hardcoded to sha256)
    """
    url = get_download_url(url_object)
    try:
        if not is_safe_url(url):
            return False, {}

        for _ in range(int(os.getenv("REQUEST_RETRIES", 1))):
            result, extra_data = _get_result_from_url(
                url_object,
                with_checksum=with_checksum,
            )
            if result and result.status_code == 200:
                break

        if result.status_code == 200:
            content_type = result.headers.get("Content-Type")
            content_length = result.headers.get("Content-Length")
            content_range = result.headers.get("Content-Range")

            if not content_length and content_range:
                # sometimes servers send content-range instead
                try:
                    content_length = content_range.split("-")[1]
                except IndexError:
                    pass

            if content_type:
                try:
                    content_type = content_type.split(";")[0]
                except IndexError:
                    pass

            if content_type or content_length:
                details = {
                    "contentLength": content_length or "",
                    "contentType": content_type or "",
                }

                if extra_data:
                    details.update(extra_data)

                return True, details
    except requests.exceptions.RequestException:
        pass

    return False, {}


def _get_result_from_url(url_object, with_checksum=False):
    method = url_object.get("method", "GET")
    headers = url_object.get("headers", {})
    url = get_download_url(url_object)

    lightweight_methods = [] if method.lower() == "post" else ["head", "options"]
    heavyweight_method = method.lower()

    for method in lightweight_methods:
        func = getattr(requests, method)
        result = func(
            url,
            timeout=REQUEST_TIMEOUT,
            headers=headers,
            params=format_userdata(url_object.get("userdata")),
        )

        if (
            not with_checksum
            and result.status_code == 200
            and (
                result.headers.get("Content-Type")
                or result.headers.get("Content-Range")
            )
            and result.headers.get("Content-Length")
        ):
            return result, {}

    func = getattr(requests, heavyweight_method)
    func_args = {"url": url, "stream": True, "headers": headers}

    if "userdata" in url_object:
        if heavyweight_method != "post":
            func_args["params"] = format_userdata(url_object.get("userdata"))
        else:
            func_args["json"] = format_userdata(url_object.get("userdata"))

    if not with_checksum:
        # fallback on GET request
        func_args["timeout"] = REQUEST_TIMEOUT
        return func(**func_args), {}

    sha = hashlib.sha256()

    with func(**func_args) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
            sha.update(chunk)

    return r, {"checksum": sha.hexdigest(), "checksumType": "sha256"}


def format_userdata(userdata):
    if not userdata:
        return None

    if not isinstance(userdata, dict):
        try:
            return json.loads(userdata)
        except json.decoder.JSONDecodeError:
            logger.info(
                "Can not decode sent userdata for asset, sending without extra parameters."
            )
            return {}

    return userdata
