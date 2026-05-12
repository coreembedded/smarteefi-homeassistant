import logging
import aiohttp
import psutil
import socket
import os
import stat
import asyncio
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from .const import DOMAIN, ARCH, OS, SYNC_INTERVAL, INITIAL_SYNC_INTERVAL

_LOGGER = logging.getLogger(__name__)

API_URL = "https://www.smarteefi.com/api/homeassistant_v1/user/devices"

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Smarteefi integration."""
    _LOGGER.debug("Setting up Smarteefi integration")
    hass.data.setdefault(DOMAIN, {})

    # Get correct integration path
    INTEGRATION_PATH = hass.config.path(f"custom_components/smarteefi")

    # Full path to HACLI binary
    if(OS=='win'):
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli.exe")
    else:
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli")

    set_executable_permissions(HACLI)
    _LOGGER.debug(f"Using HACLI path: {HACLI}")

    async def handle_refresh_devices(call):
        """Handle the service call to refresh devices."""
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await async_refresh_devices(hass, entry)

    async def handle_sync_states(call):
        """Handle the service call to sync states."""
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        coordinator = hass.data[DOMAIN].get("coordinator")
        if coordinator:
            await coordinator._async_update()

    hass.services.async_register(DOMAIN, "discover_devices", handle_refresh_devices)
    hass.services.async_register(DOMAIN, "sync_states", handle_sync_states)
    return True

class SmarteefiDataUpdateCoordinator:
    """Class to manage fetching Smarteefi data with non-blocking startup."""

    def __init__(self, hass, entry):
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self._unsub_interval = None
        self._unsub_init = None  # For the initial delayed update
        self._listeners = []
        self._hacli_path = None
        self._is_initial_sync = True
        self.ip_address = entry.data.get("ip_address")
        self.netmask = entry.data.get("netmask")
        self._is_initial_load = True  # Track initial load state

    async def async_init(self):
        """Initialize the coordinator without blocking startup."""
        # Get correct integration path
        INTEGRATION_PATH = self.hass.config.path(f"custom_components/smarteefi")

        # Full path to HACLI binary
        if(OS=='win'):
            self._hacli_path = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli.exe")
        else:
            self._hacli_path = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli")

        set_executable_permissions(self._hacli_path)
        _LOGGER.debug(f"Using HACLI path: {self._hacli_path}")

        # Track initial sync state
        self._is_initial_sync = True

        # Start with initial sync interval
        self._setup_interval(INITIAL_SYNC_INTERVAL)

    def _setup_interval(self, interval_seconds):
        """Setup or update the sync interval."""
        # Remove previous interval if exists
        if self._unsub_interval:
            self._unsub_interval()

        # Set up new interval
        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._async_update,
            timedelta(seconds=interval_seconds)
        )
        _LOGGER.debug(f"Set sync interval to {interval_seconds} seconds")

    async def _async_update(self, now=None):

        if not self.hass.is_running:
            _LOGGER.debug("HA Not Yet Ready. Wait to load")
            return

        # If this was the initial sync, switch to regular interval
        if self._is_initial_sync:
            self._is_initial_sync = False
            self._setup_interval(SYNC_INTERVAL)

        _LOGGER.debug("Performing periodic state sync for all Smarteefi devices")
        await self.async_sync_states()


    async def async_sync_states(self, entity_id=None):
        """Sync states for all devices or a specific entity."""
        devices = self.entry.data.get("devices", [])
        if not devices:
            _LOGGER.debug("No devices to sync")
            return

        if entity_id:
            # Sync only the specified entity
            entity_registry = er.async_get(self.hass)
            if entity_entry := entity_registry.async_get(entity_id):
                device = next((d for d in devices if d["id"] == entity_entry.unique_id), None)
                if device:
                    await self._sync_device_state(device)
        else:
            combined_devices = {}
            combined_cloudids = {}

            for device in devices:
                parts = device["id"].split(':')
                prefix = ':'.join(parts[:2])  # First two parts as key
                value = int(parts[2])         # Third part as integer

                if prefix in combined_devices:
                    combined_devices[prefix] |= value
                else:
                    combined_devices[prefix] = value
                    combined_cloudids[prefix] = device.get("cloudid", "")  # preserve cloudid

            # Create the new list of devices with combined IDs, preserving cloudid
            new_devices = [
                {"id": f"{prefix}:{value}", "cloudid": combined_cloudids[prefix]}
                for prefix, value in combined_devices.items()
            ]

            for index, device in enumerate(new_devices, 1):
                _LOGGER.debug(f"Processing device {index} of {len(new_devices)}")
                await self._sync_device_state(device, devices)
                await asyncio.sleep(1)  # Brief pause between devices

    async def _sync_device_state(self, device, devices):
        """Sync state for a single device."""
        command = [
            self.ip_address,
            self.netmask,
            "get-status",
            device["id"],
            str(device.get("cloudid", ""))
        ]

        _LOGGER.debug(f"Syncing state for device {device['id']}")

        try:
            process = await asyncio.create_subprocess_exec(
                self._hacli_path, *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                output = stdout.decode().strip()
                _LOGGER.debug(f"State sync successful for {device['id']}: {output}")

                try:
                    status = int(output)
                    parts = device["id"].split(':')
                    prefix = ':'.join(parts[:2])

                    if len(parts) == 3:
                        for dev in devices:
                            dev_parts = dev['id'].split(':')
                            dev_prefix = ':'.join(dev_parts[:2])  # First two parts of device ID

                            # Check if prefixes match
                            if dev_prefix == prefix:
                                # Create var1 and var2
                                entity_match_id = f"{dev_parts[0]}:{dev_parts[2]}"  # First part and third part
                                async_dispatcher_send(
                                    self.hass,
                                    f"{DOMAIN}_device_update_{entity_match_id}",
                                    {"smap": int(dev_parts[2]), "status": status}
                                )
                except ValueError:
                    if output.strip() == "Device Offline":
                        _LOGGER.debug(f"Device {device['id']} is offline, skipping state update")
                    else:
                        _LOGGER.error(f"Invalid status output for {device['id']}: {output}")
            else:
                _LOGGER.error(f"State sync failed for {device['id']}: {stderr.decode().strip()}")
        except Exception as e:
            _LOGGER.error(f"Error syncing state for {device['id']}: {e}")

    async def async_unload(self):
        """Unload the coordinator."""
        if self._unsub_init:
            self._unsub_init.cancel()
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None

async def start_udp_server(hass: HomeAssistant, ip_address: str, port: int):
    """Start UDP server to listen for device updates."""
    loop = asyncio.get_event_loop()

    class SmarteefiUDPProtocol:
        def __init__(self):
            self.transport = None

        def connection_made(self, transport):
            self.transport = transport
            sock = transport.get_extra_info('socket')
            try:
                # Enable broadcast and reuse address
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if os.name != 'nt':  # Not available on Windows
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                _LOGGER.debug("UDP socket configured for broadcast")
            except Exception as e:
                _LOGGER.error(f"Error configuring socket: {e}")

        def datagram_received(self, data, addr):
            """Handle incoming UDP packets with custom binary format."""
            try:
                _LOGGER.debug(f"Received UDP packet from {addr}: {data.hex()}")

                # Verify minimum packet length (26 bytes)
                if len(data) < 26:
                    _LOGGER.error(f"Packet too short: {len(data)} bytes")
                    return

                # Parse serial number (first 16 bytes, null-terminated)
                serial_bytes = data[:16]
                serial = serial_bytes.split(b'\x00')[0].decode('ascii')

                # Verify separators
                if data[16] != ord(':') or data[21] != ord(':'):
                    _LOGGER.error("Invalid packet format - missing separators")
                    return

                # Parse smap (4 bytes little-endian)
                smap = int.from_bytes(data[17:21], byteorder='little', signed=False)

                # Parse status (4 bytes little-endian)
                status = int.from_bytes(data[22:26], byteorder='little', signed=False)

                _LOGGER.debug(f"Parsed packet - Serial: {serial}, Smap: {smap}, Status: {status}")

                # Create the entity matching pattern (serial:smap)
                entity_match_id = f"{serial}:{smap}"

                # Signal all platforms to update
                async_dispatcher_send(
                    hass,
                    f"{DOMAIN}_device_update_{entity_match_id}",
                    {
                        "smap": smap,
                        "status": status
                    }
                )

            except Exception as e:
                _LOGGER.error(f"Error processing UDP packet: {e}")

    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: SmarteefiUDPProtocol(),
            local_addr=('0.0.0.0', port))

        _LOGGER.info(f"Started UDP server on {ip_address}:{port}")
        return transport
    except Exception as e:
        _LOGGER.error(f"Failed to start UDP server: {e}")
        return None

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Smarteefi integration from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    apitoken = entry.data.get("apitoken")

    if not apitoken:
        _LOGGER.error("API token is missing in config entry")
        return False

    # Auto-detect the active interface, IP address, and netmask
    network_interface, ip_address, netmask = _get_active_interface_ip_and_netmask()
    if not network_interface or not ip_address or not netmask:
        _LOGGER.error("Unable to determine active network interface, IP address, or netmask")
        return False

    _LOGGER.debug(f"Detected active interface: {network_interface}, IP: {ip_address}, Netmask: {netmask}")

    # Update the config entry with the latest interface details
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "network_interface": network_interface, "ip_address": ip_address, "netmask": netmask}
    )

    # Start UDP server
    udp_port = 8890  # Default port
    udp_transport = await start_udp_server(hass, ip_address, udp_port)
    if udp_transport is None:
        _LOGGER.warning("UDP server failed to start, continuing without real-time updates")

    hass.data[DOMAIN]["udp_transport"] = udp_transport

    # Initialize data coordinator
    coordinator = SmarteefiDataUpdateCoordinator(hass, entry)
    await coordinator.async_init()
    hass.data[DOMAIN]["coordinator"] = coordinator

    session = async_get_clientsession(hass)

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    # Check if devices are already loaded in config_entry
    if "devices" not in entry.data:
        try:
            _LOGGER.debug("Fetching devices using API token: %s", apitoken)
            devices = await fetch_devices(session, apitoken)
            _LOGGER.debug("Devices fetched successfully: %s", devices)

            # Update config_entry with fetched devices
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, "devices": devices}
            )

            _LOGGER.info("Smarteefi devices discovered: %s", devices)

            # Forward setup to all platforms at once
            await hass.config_entries.async_forward_entry_setups(entry, ["switch", "fan", "light", "cover"])

        except Exception as e:
            _LOGGER.error("Error fetching Smarteefi devices: %s", e)
            return False
    else:
        # Devices are already in config_entry, no need to fetch
        await hass.config_entries.async_forward_entry_setups(entry, ["switch", "fan", "light", "cover"])

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if "udp_transport" in hass.data[DOMAIN] and hass.data[DOMAIN]["udp_transport"]:
        hass.data[DOMAIN]["udp_transport"].close()
        _LOGGER.info("Stopped UDP server")

    if "coordinator" in hass.data[DOMAIN]:
        coordinator = hass.data[DOMAIN]["coordinator"]
        await coordinator.async_unload()

    # Unload all platforms
    await hass.config_entries.async_forward_entry_unload(entry, ["switch", "fan", "light", "cover"])
    return True

def _get_interface_ip_and_netmask(interface):
    """Get the IP address and netmask for the specified interface."""
    try:
        addrs = psutil.net_if_addrs().get(interface, [])
        for addr in addrs:
            if addr.family == socket.AF_INET:
                return addr.address, addr.netmask
        _LOGGER.warning(f"No IPv4 address found for interface: {interface}")
        return None, None
    except Exception as e:
        _LOGGER.error(f"Error retrieving IP address and netmask for interface {interface}: {e}")
        return None, None

def _get_active_interface_ip_and_netmask():
    """Determine the active interface with the default gateway and return its name, IP, and netmask."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]

        interfaces = psutil.net_if_addrs()
        for iface, addrs in interfaces.items():
            if iface in ['lo', 'lo0']:
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address == local_ip:
                    return iface, addr.address, addr.netmask

        _LOGGER.warning("No active interface found matching the default gateway IP.")
        return None, None, None
    except Exception as e:
        _LOGGER.error(f"Error determining active interface: {e}")
        return None, None, None

async def fetch_devices(session: aiohttp.ClientSession, apitoken: str):
    """Fetch devices from Smarteefi API using POST request."""
    payload = {"UserDevice": {"hatoken": apitoken}}
    headers = {"Content-Type": "application/json"}

    async with session.post(API_URL, json=payload, headers=headers) as response:
        response_text = await response.text()

        if response.status != 200:
            _LOGGER.error("Failed to fetch devices, status: %s, response: %s", response.status, response_text)
            return []

        json_response = await response.json()
        if json_response.get("result") != "success":
            _LOGGER.error("API returned failure: %s", json_response)
            return []

        return json_response.get("devices", [])

async def async_refresh_devices(hass: HomeAssistant, entry: ConfigEntry):
    """Refresh devices from Smarteefi API."""
    session = async_get_clientsession(hass)
    apitoken = entry.data.get("apitoken")

    if not apitoken:
        _LOGGER.error("API token is missing in config entry")
        return False

    try:
        _LOGGER.debug("Refreshing devices using API token: %s", apitoken)
        devices = await fetch_devices(session, apitoken)
        _LOGGER.debug("Devices refreshed successfully: %s", devices)

        # Get the current devices from the config entry
        current_devices = entry.data.get("devices", [])
        current_device_ids = {device['id'] for device in current_devices}
        new_device_ids = {device['id'] for device in devices}

        # Identify removed and added devices
        removed_device_ids = current_device_ids - new_device_ids
        added_device_ids = new_device_ids - current_device_ids

        # Update config_entry with refreshed devices
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "devices": devices}
        )

        # Remove entities for devices that are no longer present
        entity_registry = er.async_get(hass)
        entities_to_remove = [
            entity_entry.entity_id
            for entity_entry in entity_registry.entities.values()
            if entity_entry.config_entry_id == entry.entry_id and entity_entry.unique_id in removed_device_ids
        ]

        for entity_id in entities_to_remove:
            entity_registry.async_remove(entity_id)

        _LOGGER.info("Smarteefi devices refreshed: %s", devices)

        # Reload platforms to reflect new devices
        await hass.config_entries.async_forward_entry_unload(entry, ["switch", "fan", "light", "cover"])
        await hass.config_entries.async_forward_entry_setups(entry, ["switch", "fan", "light", "cover"])

    except Exception as e:
        _LOGGER.error("Error refreshing Smarteefi devices: %s", e)
        return False

def set_executable_permissions(file_path: str) -> None:
    """Set executable permissions on the specified file."""
    if os.path.exists(file_path):
        current_perms = os.stat(file_path).st_mode
        os.chmod(file_path, current_perms | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    else:
        _LOGGER.error(f"CLI not found at {file_path}")
