import logging
import os
import asyncio
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, ARCH, OS

_LOGGER = logging.getLogger(__name__)

_LOGGER.info(f"Domain is {DOMAIN}")


def _extract_serial(unique_id: str) -> str | None:
    """Return device serial from unique id format serial:ignored:smap."""
    parts = unique_id.split(":")
    if len(parts) != 3:
        return None
    return parts[0]


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smarteefi switches from a config entry."""
    # Get correct integration path
    INTEGRATION_PATH = hass.config.path(f"custom_components/smarteefi")

    # Full path to HACLI binary
    if OS == "win":
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli.exe")
    else:
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli")

    _LOGGER.debug(f"Using HACLI path: {HACLI}")

    devices = entry.data.get("devices", [])

    if not devices:
        _LOGGER.error("No devices found for Smarteefi switch.")
        return

    # Get host ip address from config_entry
    ip_address = entry.data.get("ip_address")

    if not ip_address:
        _LOGGER.error("ip_address not found in config entry!")
        return

    # Get host ip address from config_entry
    netmask = entry.data.get("netmask")

    if not netmask:
        _LOGGER.error("netmask not found in config entry!")
        return

    # Calculate socket count per physical strip serial
    serial_counts = {}
    for device in devices:
        if device.get("type") == "switch":
            serial = _extract_serial(device.get("id", ""))
            if serial:
                serial_counts[serial] = serial_counts.get(serial, 0) + 1

    switches = []
    for device in devices:
        if device.get("type") == "switch":
            unique_id = device.get("id", "")
            socket_count = 1  # Default fallback

            serial = _extract_serial(unique_id)
            if serial:
                socket_count = serial_counts.get(serial, 1)

            switches.append(SmarteefiSwitch(device, HACLI, ip_address, netmask, socket_count))

    async_add_entities(switches, True)


class SmarteefiSwitch(SwitchEntity):
    """Representation of a Smarteefi switch."""

    def __init__(self, device, hacli_path, ip_address, netmask, socket_count: int = 1):
        self._device = device
        self._state = False
        self._name = device.get("name", "Unnamed Switch")
        self._unique_id = device.get("id", "")  # Format: "serial:ignored:smap"
        self._cloud_id = device.get("cloudid", "")
        self._hacli = hacli_path
        self.ip_address = ip_address
        self.netmask = netmask
        self._update_unsub = None
        self._smap = None  # Store smap value from entity ID
        self._serial = "unknown"  # Store the shared hardware serial
        self._socket_count = socket_count  # Dynamic socket count

        # Extract serial:smap from unique_id (format: "serial:ignored:smap")
        parts = self._unique_id.split(":")
        if len(parts) == 3:
            self._serial = parts[0]
            self._entity_match_id = f"{parts[0]}:{parts[2]}"  # serial:smap
            try:
                self._smap = int(parts[2])
            except (ValueError, TypeError):
                _LOGGER.error(f"Invalid smap integer in unique_id: {self._unique_id}")
        else:
            _LOGGER.error(f"Invalid unique_id format: {self._unique_id}")

    async def async_added_to_hass(self):
        """Register update dispatcher."""
        if hasattr(self, "_entity_match_id"):
            self._update_unsub = async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_device_update_{self._entity_match_id}",
                self._handle_device_update,
            )

    async def async_will_remove_from_hass(self):
        """Unregister update dispatcher."""
        if self._update_unsub:
            self._update_unsub()

    def _handle_device_update(self, data):
        """Update state from UDP message."""
        received_smap = data.get("smap")
        status = data.get("status")

        # Only process if smap matches our entity's smap
        if received_smap != self._smap:
            return

        # Smap bitwise check: ON if smap matches status bitmask, OFF if status is 0
        new_state = bool((status != 0) and (received_smap & status))

        if self._state != new_state:
            self._state = new_state
            self.schedule_update_ha_state()
            _LOGGER.debug(
                f"Updated switch {self._name} - "
                f"State: {'on' if new_state else 'off'}, "
                f"Smap: {received_smap}, Status: {status}"
            )

    @property
    def name(self):
        """Return the name of the switch."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID for the switch."""
        return self._unique_id

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information to link sockets dynamically to a power strip device."""
        if self._serial == "unknown":
            return None

        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            # Append last 4 chars of serial so multiple power strips have identifiable names
            name=f"Smarteefi Strip ({self._serial[-4:]})",
            manufacturer="Smarteefi",
            # Dynamically describe the power strip by socket count
            model=f"{self._socket_count}x Sockets Smart Power Strip",
        )

    @property
    def is_on(self):
        """Return True if the switch is on."""
        return self._state

    async def _execute_cli(self, command):
        """Run the HACLI binary with the given command."""
        full_command = [self._hacli] + command

        _LOGGER.debug(f"Executing CLI command: {' '.join(full_command)}")

        try:
            process = await asyncio.create_subprocess_exec(
                self._hacli,
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                _LOGGER.debug(
                    f"Command succeeded: {' '.join(full_command)}, Output: {stdout.decode().strip()}"
                )
                return True
            else:
                _LOGGER.error(
                    f"Command failed: {' '.join(full_command)}, Error: {stderr.decode().strip()}"
                )
                return False
        except FileNotFoundError:
            _LOGGER.error(f"CLI binary not found at {self._hacli}")
            return False
        except Exception as e:
            _LOGGER.error(f"Error executing CLI: {e}")
            return False

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        _LOGGER.info("Turning on Smarteefi switch: %s", self._name)
        if await self._execute_cli(
            [self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "1"]
        ):
            self._state = True
            self.schedule_update_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        _LOGGER.info("Turning off Smarteefi switch: %s", self._name)
        if await self._execute_cli(
            [self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "0"]
        ):
            self._state = False
            self.schedule_update_ha_state()
