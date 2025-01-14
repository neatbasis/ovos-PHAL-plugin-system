import os
import shutil
import subprocess
from os.path import dirname, join
from threading import Event

from json_database import JsonStorageXDG, JsonDatabaseXDG
from ovos_bus_client.message import Message
from ovos_backend_client.identity import IdentityManager
from ovos_config.config import Configuration, update_mycroft_config
from ovos_config.locale import set_default_lang
from ovos_config.locations import OLD_USER_CONFIG, USER_CONFIG, WEB_CONFIG_CACHE
from ovos_config.meta import get_xdg_base

from ovos_plugin_manager.phal import AdminPlugin, PHALPlugin
from ovos_plugin_manager.templates.phal import PHALValidator, AdminValidator
from ovos_utils import classproperty
from ovos_bus_client.apis.gui import GUIInterface
from ovos_utils.process_utils import RuntimeRequirements
from ovos_utils.system import is_process_running, check_service_active, \
    check_service_installed, restart_service
from ovos_utils.xdg_utils import xdg_state_home, xdg_cache_home, xdg_data_home
from ovos_utils.log import LOG


class SystemEventsValidator(PHALValidator):
    @staticmethod
    def validate(config=None):
        """ this method is called before loading the plugin.
        If it returns False the plugin is not loaded.
        This allows a plugin to run platform checks"""
        # check if admin plugin is not enabled
        cfg = Configuration().get("PHAL", {}).get("admin", {})
        if cfg.get("ovos-PHAL-plugin-system", {}).get("enabled"):
            # run this plugin in admin mode (as root)
            return False

        LOG.info("ovos-PHAL-plugin-system running as user")
        return True


class SystemEvents(PHALPlugin):
    validator = SystemEventsValidator

    def __init__(self, bus=None, config=None):
        super().__init__(bus=bus, name="ovos-PHAL-plugin-system", config=config)
        self.gui = GUIInterface(bus=self.bus, skill_id=self.name,
                                config=self.config_core.get('gui'))

        self.bus.on("system.ntp.sync", self.handle_ntp_sync_request)
        self.bus.on("system.ssh.status", self.handle_ssh_status)
        self.bus.on("system.ssh.enable", self.handle_ssh_enable_request)
        self.bus.on("system.ssh.disable", self.handle_ssh_disable_request)
        self.bus.on("system.reboot", self.handle_reboot_request)
        self.bus.on("system.shutdown", self.handle_shutdown_request)
        self.bus.on("system.factory.reset", self.handle_factory_reset_request)
        self.bus.on("system.factory.reset.register", self.handle_reset_register)
        self.bus.on("system.configure.language",
                    self.handle_configure_language_request)
        self.bus.on("system.mycroft.service.restart",
                    self.handle_mycroft_restart_request)

        self.core_service_name = config.get("core_service") or "ovos.service"
        # In Debian, ssh stays active, but sshd is removed when ssh is disabled
        self.ssh_service = config.get("ssh_service") or "sshd.service"
        self.use_root = config.get("sudo", True)

        self.factory_reset_plugs = []

        # trigger register events from phal plugins
        self.bus.emit(Message("system.factory.reset.ping"))

    @classproperty
    def runtime_requirements(self):
        return RuntimeRequirements(internet_before_load=False,
                                   network_before_load=False,
                                   requires_internet=False,
                                   requires_network=False,
                                   no_internet_fallback=True,
                                   no_network_fallback=True)

    @property
    def use_external_factory_reset(self):
        # see if PHAL service / mycroft.conf requested external handling
        external_requested = self.config.get("use_external_factory_reset")
        # auto detect ovos-shell if no explicit preference
        if external_requested is None and is_process_running("ovos-shell"):
            return True
        return external_requested or False

    def handle_reset_register(self, message):
        if not message.data.get("skill_id"):
            LOG.warning(f"Got registration request without a `skill_id`: "
                        f"{message.data}")
            if any((x in message.data for x in ('reset_hardware', 'wipe_cache',
                                                'wipe_config', 'wipe_data',
                                                'wipe_logs'))):
                LOG.warning(f"Deprecated reset request from GUI")
                self.handle_factory_reset_request(message)
            return
        sid = message.data["skill_id"]
        if sid not in self.factory_reset_plugs:
            self.factory_reset_plugs.append(sid)

    def handle_factory_reset_request(self, message):
        LOG.debug(f'Factory reset request: {message.data}')
        self.bus.emit(message.forward("system.factory.reset.start"))
        self.bus.emit(message.forward("system.factory.reset.ping"))

        if os.path.isfile(IdentityManager.OLD_IDENTITY_FILE):
            os.remove(IdentityManager.OLD_IDENTITY_FILE)
        if os.path.isfile(IdentityManager.IDENTITY_FILE):
            os.remove(IdentityManager.IDENTITY_FILE)

        wipe_cache = message.data.get("wipe_cache", True)
        if wipe_cache:
            p = f"{xdg_cache_home()}/{get_xdg_base()}"
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)

        wipe_data = message.data.get("wipe_data", True)
        if wipe_data:
            p = f"{xdg_data_home()}/{get_xdg_base()}"
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)

            # misc json databases from offline/personal backend
            for j in ["ovos_device_info",
                      "ovos_oauth",
                      "ovos_oauth_apps",
                      "ovos_devices",
                      "ovos_metrics",
                      "ovos_preferences",
                      "ovos_skills_meta"]:
                p = JsonStorageXDG(j).path
                if os.path.isfile(p):
                    os.remove(p)
            for j in ["ovos_metrics",
                      "ovos_utterances",
                      "ovos_wakewords"]:
                p = JsonDatabaseXDG(j).db.path
                if os.path.isfile(p):
                    os.remove(p)

        wipe_logs = message.data.get("wipe_logs", True)
        if wipe_logs:
            p = f"{xdg_state_home()}/{get_xdg_base()}"
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)

        wipe_cfg = message.data.get("wipe_configs", True)
        if wipe_cfg:
            if os.path.isfile(OLD_USER_CONFIG):
                os.remove(OLD_USER_CONFIG)
            if os.path.isfile(USER_CONFIG):
                os.remove(USER_CONFIG)
            if os.path.isfile(WEB_CONFIG_CACHE):
                os.remove(WEB_CONFIG_CACHE)

        LOG.debug("Data reset completed")

        reset_phal = message.data.get("reset_hardware", True)
        if reset_phal and len(self.factory_reset_plugs):
            LOG.debug(f"Wait for reset plugins: {self.factory_reset_plugs}")
            reset_plugs = []
            event = Event()

            def on_done(message):
                nonlocal reset_plugs, event
                sid = message.data["skill_id"]
                if sid not in reset_plugs:
                    reset_plugs.append(sid)
                if all([s in reset_plugs for s in self.factory_reset_plugs]):
                    event.set()

            self.bus.on("system.factory.reset.phal.complete", on_done)
            self.bus.emit(message.forward("system.factory.reset.phal",
                                          message.data))
            event.wait(timeout=60)
            self.bus.remove("system.factory.reset.phal.complete", on_done)

        script = message.data.get("script", True)
        if script:
            script = os.path.expanduser(self.config.get("reset_script", ""))
            LOG.debug(f"Running reset script: {script}")
            if os.path.isfile(script):
                if self.use_external_factory_reset:
                    self.bus.emit(Message("ovos.shell.exec.factory.reset",
                                          {"script": script}))
                    # OVOS shell will handle all external operations here to
                    # exec script including sending complete event to whoever
                    # is listening
                else:
                    subprocess.call(script, shell=True)
                    self.bus.emit(
                        message.forward("system.factory.reset.complete"))

        reboot = message.data.get("reboot", True)
        if reboot:
            self.bus.emit(message.forward("system.reboot"))

    def handle_ssh_enable_request(self, message):
        subprocess.call(f"systemctl enable {self.ssh_service}", shell=True)
        subprocess.call(f"systemctl start {self.ssh_service}", shell=True)
        # ovos-shell does not want to display
        if message.data.get("display", True):
            page = join(dirname(__file__), "ui", "Status.qml")
            self.gui["status"] = "Enabled"
            self.gui["label"] = "SSH Enabled"
            self.gui.show_page(page)

    def handle_ssh_disable_request(self, message):
        subprocess.call(f"systemctl stop {self.ssh_service}", shell=True)
        subprocess.call(f"systemctl disable {self.ssh_service}", shell=True)
        # ovos-shell does not want to display
        if message.data.get("display", True):
            page = join(dirname(__file__), "ui", "Status.qml")
            self.gui["status"] = "Disabled"
            self.gui["label"] = "SSH Disabled"
            self.gui.show_page(page)

    def handle_ntp_sync_request(self, message):
        """
        Force the system clock to synchronize with internet time servers
        """
        # Check to see what service is installed
        if check_service_installed('ntp'):
            subprocess.call('service ntp stop', shell=True)
            subprocess.call('ntpd -gq', shell=True)
            subprocess.call('service ntp start', shell=True)
        elif check_service_installed('systemd-timesyncd'):
            subprocess.call("systemctl stop systemd-timesyncd", shell=True)
            subprocess.call("systemctl start systemd-timesyncd", shell=True)
        if check_service_active('ntp') or check_service_active('systemd-timesyncd'):
            # NOTE: this one defaults to False
            # it is usually part of other groups of actions that may
            # provide their own UI
            if message.data.get("display", False):
                page = join(dirname(__file__), "ui", "Status.qml")
                self.gui["status"] = "Enabled"
                self.gui["label"] = "Clock updated"
                self.gui.show_page(page)
            self.bus.emit(message.reply('system.ntp.sync.complete'))
        else:
            LOG.debug("No time sync service installed")

    def handle_reboot_request(self, message):
        """
        Shut down and restart the system
        """
        if message.data.get("display", True):
            page = join(dirname(__file__), "ui", "Reboot.qml")
            self.gui.show_page(page, override_animations=True,
                               override_idle=True)

        script = os.path.expanduser(self.config.get("reboot_script") or "")
        LOG.info(f"Reboot requested. script={script}")
        if script and os.path.isfile(script):
            subprocess.call(script, shell=True)
        else:
            subprocess.call("systemctl reboot -i", shell=True)

    def handle_shutdown_request(self, message):
        """
        Turn the system completely off (with no option to inhibit it)
        """
        if message.data.get("display", True):
            page = join(dirname(__file__), "ui", "Shutdown.qml")
            self.gui.show_page(page, override_animations=True,
                               override_idle=True)
        script = os.path.expanduser(self.config.get("shutdown_script") or "")
        LOG.info(f"Shutdown requested. script={script}")
        if script and os.path.isfile(script):
            subprocess.call(script, shell=True)
        else:
            subprocess.call("systemctl poweroff -i", shell=True)

    def handle_configure_language_request(self, message):
        language_code = message.data.get('language_code', "en_US")
        with open(f"{os.environ['HOME']}/.bash_profile",
                  "w") as bash_profile_file:
            bash_profile_file.write(f"export LANG={language_code}\n")

        language_code = language_code.lower().replace("_", "-")
        set_default_lang(language_code)
        update_mycroft_config({"lang": language_code}, bus=self.bus)

        # NOTE: this one defaults to False
        # it is usually part of other groups of actions that may
        # provide their own UI
        if message.data.get("display", False):
            page = join(dirname(__file__), "ui", "Status.qml")
            self.gui["status"] = "Enabled"
            self.gui["label"] = f"Language changed to {language_code}"
            self.gui.show_page(page)

        self.bus.emit(Message('system.configure.language.complete',
                              {"lang": language_code}))

    def handle_mycroft_restart_request(self, message):
        if message.data.get("display", True):
            page = join(dirname(__file__), "ui", "Restart.qml")
            self.gui.show_page(page, override_animations=True,
                               override_idle=True)
        service = self.core_service_name
        try:
            restart_service(service, sudo=False, user=True)
        except:
            try:
                restart_service(service, sudo=True, user=False)
            except:
                LOG.error("No mycroft or ovos service installed")
                return False

    def handle_ssh_status(self, message):
        """
        Check SSH service status and emit a response
        """
        enabled = check_service_active(self.ssh_service)
        self.bus.emit(message.response(data={'enabled': enabled}))

    def shutdown(self):
        self.bus.remove("system.ntp.sync", self.handle_ntp_sync_request)
        self.bus.remove("system.ssh.enable", self.handle_ssh_enable_request)
        self.bus.remove("system.ssh.disable", self.handle_ssh_disable_request)
        self.bus.remove("system.reboot", self.handle_reboot_request)
        self.bus.remove("system.shutdown", self.handle_shutdown_request)
        self.bus.remove("system.factory.reset",
                        self.handle_factory_reset_request)
        self.bus.remove("system.factory.reset.register",
                        self.handle_reset_register)
        self.bus.remove("system.configure.language",
                        self.handle_configure_language_request)
        self.bus.remove("system.mycroft.service.restart",
                        self.handle_mycroft_restart_request)
        super().shutdown()

class SystemEventsAdminValidator(AdminValidator, SystemEventsValidator):
    @staticmethod
    def validate(config=None):
        LOG.info("ovos-PHAL-plugin-system running as root")
        return True

class SystemEventsAdminPlugin(AdminPlugin, SystemEventsPlugin):
    validator = SystemEventsAdminValidator
