import asyncio
import socket
import logging
import subprocess
import threading
import time
import sys
import platform
from app.config import load_config

REDIRECT_PORT = 9999

# Thread-safe dictionary: (phone_ip, phone_port) -> (original_dst_ip, original_dst_port)
active_connections = {}
connections_lock = threading.Lock()

_stop_event = None
_divert_thread = None
_windivert_handle = None
_tcp_server = None

def get_lan_ip(interface_name):
    cmd = f'powershell -NoProfile -Command "(Get-NetIPAddress -InterfaceAlias \'{interface_name}\' -AddressFamily IPv4).IPAddress"'
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        ip = res.stdout.strip()
        if ip and not ip.startswith("Error") and not "not found" in ip.lower():
            return ip
    except Exception as e:
        logging.error(f"[DivertRouter] Error getting LAN IP for {interface_name}: {e}")
    return "192.168.137.1" # Fallback

def get_proxy_port_for_ip(phone_ip):
    config = load_config()
    for dev in config.devices:
        if dev.ip == phone_ip:
            if dev.proxy_id:
                return dev.proxy_port
    return None

async def pipe_sockets(r1, w1, r2, w2):
    async def forward(r, w):
        try:
            while True:
                data = await r.read(65536)  # 64KB buffer: giảm syscall, tăng throughput
                if not data:
                    break
                w.write(data)
                await w.drain()
        except Exception:
            pass
        finally:
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass

    await asyncio.gather(
        forward(r1, w2),
        forward(r2, w1),
        return_exceptions=True
    )


async def handle_client(reader, writer):
    peer = writer.get_extra_info('peername')
    if not peer:
        writer.close()
        return

    phone_ip, phone_port = peer
    
    # Retrieve original destination
    dest = None
    with connections_lock:
        dest = active_connections.get((phone_ip, phone_port))

    if not dest:
        writer.close()
        return

    target_ip, target_port = dest
    proxy_port = get_proxy_port_for_ip(phone_ip)

    try:
        if proxy_port:
            # Connect via local SOCKS5 proxy
            proxy_reader, proxy_writer = await asyncio.open_connection('127.0.0.1', proxy_port)
            
            # SOCKS5 Handshake
            proxy_writer.write(b'\x05\x01\x00')
            await proxy_writer.drain()
            
            resp = await proxy_reader.readexactly(2)
            if resp[0] != 5 or resp[1] != 0:
                raise Exception("SOCKS5 auth failed")
            
            # SOCKS5 Connect
            ip_bytes = socket.inet_aton(target_ip)
            port_bytes = target_port.to_bytes(2, 'big')
            proxy_writer.write(b'\x05\x01\x00\x01' + ip_bytes + port_bytes)
            await proxy_writer.drain()
            
            # Read response
            hdr = await proxy_reader.readexactly(4)
            ver, rep, rsv, atyp = hdr
            if rep != 0:
                raise Exception(f"SOCKS5 connect failed: {rep}")
            
            if atyp == 1:
                await proxy_reader.readexactly(4 + 2)
            elif atyp == 3:
                len_byte = await proxy_reader.readexactly(1)
                await proxy_reader.readexactly(len_byte[0] + 2)
            elif atyp == 4:
                await proxy_reader.readexactly(16 + 2)
                
            await pipe_sockets(reader, writer, proxy_reader, proxy_writer)
        else:
            # Direct route
            target_reader, target_writer = await asyncio.open_connection(target_ip, target_port)
            await pipe_sockets(reader, writer, target_reader, target_writer)
    except Exception as e:
        logging.error(f"[DivertRouter] Error routing {phone_ip}:{phone_port} -> {target_ip}:{target_port}: {e}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        # Delayed cleanup
        async def delayed_cleanup():
            await asyncio.sleep(10)
            with connections_lock:
                active_connections.pop((phone_ip, phone_port), None)
        asyncio.create_task(delayed_cleanup())

def _run_divert_loop(lan_ip, lan_subnet_prefix, stop_event):
    global _windivert_handle
    if platform.system() != "Windows":
        return
        
    try:
        import pydivert
    except ImportError:
        logging.error("[DivertRouter] pydivert package not found. WinDivert loop cannot run.")
        return

    filter_str = (
        f"ip and tcp and ("
        f"  (ip.SrcAddr >= {lan_subnet_prefix}2 and ip.SrcAddr <= {lan_subnet_prefix}254 and (ip.DstAddr < {lan_subnet_prefix}0 or ip.DstAddr > {lan_subnet_prefix}255)) or"
        f"  (ip.SrcAddr == {lan_ip} and tcp.SrcPort == {REDIRECT_PORT} and ip.DstAddr >= {lan_subnet_prefix}2 and ip.DstAddr <= {lan_subnet_prefix}254)"
        f")"
    )
    logging.info(f"[DivertRouter] Starting WinDivert with filter: {filter_str}")
    
    _windivert_handle = pydivert.WinDivert(filter_str)
    try:
        _windivert_handle.open()
        logging.info("[DivertRouter] WinDivert driver loaded.")
        
        while not stop_event.is_set():
            packet = _windivert_handle.recv()
            if not packet:
                continue
                
            if packet.is_inbound:
                phone_ip = packet.src_addr
                phone_port = packet.src_port
                target_ip = packet.dst_addr
                target_port = packet.dst_port
                
                with connections_lock:
                    active_connections[(phone_ip, phone_port)] = (target_ip, target_port)
                
                packet.dst_addr = lan_ip
                packet.dst_port = REDIRECT_PORT
                packet.recalculate_checksums()
                _windivert_handle.send(packet)
                
            elif packet.is_outbound:
                phone_ip = packet.dst_addr
                phone_port = packet.dst_port
                
                dest = None
                with connections_lock:
                    dest = active_connections.get((phone_ip, phone_port))
                
                if dest:
                    target_ip, target_port = dest
                    packet.src_addr = target_ip
                    packet.src_port = target_port
                    packet.recalculate_checksums()
                    _windivert_handle.send(packet)
                else:
                    _windivert_handle.send(packet)
    except Exception as e:
        if not stop_event.is_set():
            logging.error(f"[DivertRouter] WinDivert loop exception: {e}")
    finally:
        try:
            _windivert_handle.close()
        except Exception:
            pass
        logging.info("[DivertRouter] WinDivert loop stopped.")

async def start_divert_router():
    global _stop_event, _divert_thread, _tcp_server
    
    if platform.system() != "Windows":
        return
        
    config = load_config()
    if getattr(config, "routing_mode", "tun") != "port":
        logging.info("[DivertRouter] Routing mode is not 'port'. WinDivert redirector will not start.")
        return
        
    logging.info("[DivertRouter] Starting WinDivert Redirector service...")
    
    # Stop first if running
    await stop_divert_router()
    
    lan_interface = config.lan_interface
    lan_ip = get_lan_ip(lan_interface)
    parts = lan_ip.split(".")
    if len(parts) != 4:
        logging.error(f"[DivertRouter] Invalid LAN IP parsed: {lan_ip}. Aborting start.")
        return
        
    lan_subnet_prefix = f"{parts[0]}.{parts[1]}.{parts[2]}."
    
    _stop_event = threading.Event()
    
    # Start packet loop thread
    _divert_thread = threading.Thread(
        target=_run_divert_loop,
        args=(lan_ip, lan_subnet_prefix, _stop_event),
        daemon=True
    )
    _divert_thread.start()
    
    # Start TCP redirect server in the FastAPI event loop
    try:
        _tcp_server = await asyncio.start_server(handle_client, lan_ip, REDIRECT_PORT)
        logging.info(f"[DivertRouter] TCP Redirect Server listening on {lan_ip}:{REDIRECT_PORT}")
    except Exception as e:
        logging.error(f"[DivertRouter] Failed to start TCP Redirect Server on {lan_ip}:{REDIRECT_PORT}: {e}")
        _stop_event.set()
        if _windivert_handle:
            try:
                _windivert_handle.close()
            except Exception:
                pass

async def stop_divert_router():
    global _stop_event, _divert_thread, _windivert_handle, _tcp_server
    
    if platform.system() != "Windows":
        return
        
    logging.info("[DivertRouter] Stopping WinDivert Redirector service...")
    
    if _stop_event:
        _stop_event.set()
        
    if _windivert_handle:
        try:
            _windivert_handle.close()
        except Exception as e:
            pass
        _windivert_handle = None
        
    if _tcp_server:
        try:
            _tcp_server.close()
            await _tcp_server.wait_closed()
        except Exception as e:
            pass
        _tcp_server = None
        
    if _divert_thread:
        # Wait a bit for the thread to end
        _divert_thread.join(timeout=2)
        _divert_thread = None
        
    with connections_lock:
        active_connections.clear()
        
    logging.info("[DivertRouter] WinDivert Redirector service stopped.")
