"""Bounded, read-only macOS network checks; never change network configuration."""
import ipaddress
import concurrent.futures
import re
import subprocess
import urllib.request

HOSTS = ('www.apple.com', 'connectivitycheck.gstatic.com', 'www.cloudflare.com', 'www.wikipedia.org', 'www.python.org')


def command(args, timeout=4):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def text(args):
    result = command(args)
    return result.stdout if result and result.returncode == 0 else ''


def interface():
    hardware = text(['networksetup', '-listallhardwareports'])
    match = re.search(r'Hardware Port: (?:Wi-Fi|AirPort)\nDevice: (\S+)', hardware)
    return match.group(1) if match else None


def route(destination, scope=None):
    args = ['/sbin/route', '-n', 'get']
    if scope:
        args += ['-ifscope', scope]
    output = text(args + [destination])
    return {key: value for key, value in re.findall(r'^\s*(interface|gateway):\s*(\S+)', output, re.M)}


def addresses(host):
    output = text(['/usr/bin/dscacheutil', '-q', 'host', '-a', 'name', host])
    result = []
    for value in re.findall(r'^\s*(?:ip_address|ipv6_address):\s*(\S+)', output, re.M):
        try:
            ipaddress.ip_address(value)
            result.append(value)
        except ValueError:
            pass
    return result


def diagnostics(local_ip):
    wifi = interface()
    default = route('default')
    scoped = route('default', wifi) if wifi else {}
    gateway = scoped.get('gateway')
    gateway_status = 'unknown'
    if gateway and local_ip:
        try:
            ipaddress.ip_address(gateway)
            reply = command(['/sbin/ping', '-n', '-c', '1', '-W', '1000', '-S', local_ip, gateway], timeout=2)
            if reply:
                gateway_status = 'responding' if reply.returncode == 0 else 'no-reply'
        except ValueError:
            pass
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        resolved = list(pool.map(addresses, HOSTS))
    dns = 'working' if all(resolved) else 'partial' if any(resolved) else 'failed'
    destinations = []
    for group in resolved:
        # Sample one address per family without serializing independent route checks.
        seen = set()
        for address in group:
            family = ipaddress.ip_address(address).version
            if family not in seen:
                seen.add(family)
                destinations.append(address)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(destinations))) as pool:
        routes = list(pool.map(route, destinations))
    interfaces = {item['interface'] for item in routes if item.get('interface')}
    if not interfaces and default.get('interface'):
        interfaces.add(default['interface'])
    vpn = any(name.startswith(('utun', 'ppp', 'ipsec')) for name in interfaces)
    route_status = ('unknown' if not interfaces or not wifi else
                    'vpn-route' if vpn else 'other-interface' if interfaces != {wifi} else 'wifi')
    proxy = any(value for key, value in urllib.request.getproxies().items()
                if key.lower() in ('http', 'https', 'all', 'socks', 'socks5'))
    proxy_output = text(['/usr/sbin/scutil', '--proxy'])
    proxy = proxy or bool(re.search(r'(?:HTTP|HTTPS|SOCKS|ProxyAutoConfig|ProxyAutoDiscovery)Enable\s*:\s*1', proxy_output))
    connected_vpn = bool(re.search(r'\(Connected\)', text(['/usr/sbin/scutil', '--nc', 'list'])))
    return dict(wifi_interface=wifi, gateway=gateway, gateway_status=gateway_status,
                dns=dns, route_interface=', '.join(sorted(interfaces)) or None, route_status=route_status,
                vpn='detected' if vpn or connected_vpn else 'not-detected',
                proxy='configured' if proxy else 'not-detected')
