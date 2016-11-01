from __future__ import print_function

import os
import re
import sys
import time
import uuid
import shlex
import atexit
import shutil
import subprocess
from threading import Thread, Lock
from xml.etree import ElementTree

def pprint(elem, level=0):
    ''' Pretty print for ElementTree nodes (https://stackoverflow.com/a/4590052/440780) '''
    i = "\n" + level*"  "
    j = "\n" + (level-1)*"  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
        for subelem in elem:
            subelem.pprint(level+1)
        if not elem.tail or not elem.tail.strip():
            elem.tail = j
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = j
    return elem

# Monkey-patch the pprint function for the ElementTree.Element object
ElementTree.Element.pprint = pprint


# Global variable used to restart ADB server upon initialization of module
__ADB_RESTART__ = True

# http://developer.android.com/reference/android/view/KeyEvent.html
__KEY_CODES__ = {
    'HOME': 3,
    'BACK': 4,
    'UP': 19,
    'DOWN': 20,
    'LEFT': 21,
    'RIGHT': 22,
    'CENTER': 23,
    'POWER': 26,
    'A': 29,
    'C': 31,
    'V': 50,
    'X': 52,
    'TAB': 61,
    'ENTER': 66,
    'BACKSPACE': 67,
    'MENU': 82,
    'ESC': 111,
    'DEL': 112,
    'CTRL': 113,
    'END': 123,
    'APP_SWITCH': 187,
}

class HostProcess(object):
    ''' Wrapper for executing commands in this process' shell '''
    
    def __init__(self, binary_name=None, singleton=False, debug=False):
        self.bin = self.type_check_cmd(binary_name)
        if len(self.bin) > 0 and hasattr(shutil, 'which'):
            self.bin[0] = shutil.which(self.bin[0])
            if not self.bin[0]:
                raise ValueError('Binary not found in path: "%s"' % binary_name)
        self._singleton = singleton
        self._debug = debug
        self.lock = Lock()
        self.proc_pool = []

        # Register process-level exit handler in case the process calling this gets killed
        atexit.register(self._exit_handler)

    def _exit_handler(self):
        for proc in self.proc_pool:
            try:
                proc.kill()
                output = 'Process "%s" killed because parent process is shutting down' % proc.pid
                print(output, sys.stderr)
            except OSError:
                pass

    def _print(self, *output, **kwargs):
        if self._debug and len(output) > 0:
            print(*output, **kwargs)

    @staticmethod
    def type_check_cmd(cmd):
        str_types = str if sys.version_info >= (3, 0, 0) else (str, unicode)
        if cmd is None:
            cmd = list()
        if isinstance(cmd, str_types):
            cmd = shlex.split(cmd, posix=(os.name == 'posix'))
        if not hasattr(cmd, '__iter__') or not all([isinstance(c, str_types) for c in cmd]):
            raise ValueError('Parameter "cmd" must be of type str or list of str, instead found: %r' % cmd)
        return [cmd.strip() for cmd in cmd]

    def exec_cmd(self, args, timeout=None, grep=None, callback=None):

        # If this requires current working dir change, acquire lock
        if self._singleton:
            self.lock.acquire()

        # Concatenate the final command
        cmd = self.bin + self.type_check_cmd(args)
        self._print('> ' + ' '.join(cmd))

        # Execute and parse output
        proc = subprocess.Popen(
            cmd, shell=(os.name != 'posix' and sys.version_info < (3, 0, 0)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        # Add process to internal pool
        self.proc_pool.append(proc)

        try:
            if sys.version_info >= (3, 3, 0) and timeout > 0:
                output, error = proc.communicate(timeout=timeout)
            else:
                output, error = proc.communicate()
            output = str(output.decode('utf-8').rstrip())
                
        except UnicodeDecodeError:
            pass
        except (subprocess.TimeoutExpired if sys.version_info >= (3, 3, 0) else UnicodeDecodeError):
            try:
                proc.kill()
                output = 'Process "%s" timed out after %s seconds' % (proc.pid, str(timeout))
                print(output, sys.stderr)
            except OSError:
                pass
        finally:
            # Process is done, remove from pool
            self.proc_pool.remove(proc)

            # Return to original working directory if needed
            if self._singleton:
                self.lock.release()
            
            # If grep was given, filter output
            if grep:
                rgx = re.compile(grep)
                lines = [line.rstrip() for line in output.splitlines()]
                output = '\n'.join([line for line in lines if rgx.search(line)])

        if callback:
            callback(proc.returncode, output)

        return proc.returncode, output

class ADB(HostProcess):
    ''' Wrapper for the ADB command that includes many common operations '''
    
    def __init__(self, default_target_device=None, debug=True):
        global __ADB_RESTART__

        HostProcess.__init__(self, 'adb', debug=debug)
        self.default_target_device = None
        
        # Has the ADB server been restarted at least once?
        if __ADB_RESTART__:
            self.kill_server()
            self.start_server()
            __ADB_RESTART__ = False
            
        # If we are given a device, try to connect
        if default_target_device:
            self.default_target_device = self.connect(target_device=default_target_device)

        # Internal flags
        self.pending_wakeup = False

    def run(self, cmd, grep=None, target_device=None):
        if target_device is None and self.default_target_device:
            target_device = self.default_target_device
        opt = [] if target_device is None else ['-s', target_device]
        cmd = opt + HostProcess.type_check_cmd(cmd)
        retcode, output = HostProcess.exec_cmd(self, cmd, grep=grep)
        self._print(output)
        return output

    def shell(self, cmd, grep=None, target_device=None):
        cmd = ['shell'] + HostProcess.type_check_cmd(cmd)
        return self.run(cmd, grep=grep, target_device=target_device)

    def exec_out(self, cmd, grep=None, target_device=None):
        cmd = ['exec-out'] + HostProcess.type_check_cmd(cmd)
        return self.run(cmd, grep=grep, target_device=target_device)

    def connect(self, target_device=None):
        cmd = [] if target_device is None else [target_device]

        retcode, output = HostProcess.exec_cmd(self, ['connect'] + cmd)
        output = output.lower()
        self._print('>>>', output)
        str_check = 'connected to '
        if str_check not in output:
            raise RuntimeError(output)
            
        # The actual device id might be slightly different
        device_id = output.split(str_check)[1].strip() 
        
        # If this is the first connection, make it the default_target_device
        if self.default_target_device is None:
            self.default_target_device = device_id
            
        # Wait a few seconds for the connection to become active
        time.sleep(3)
        
        return device_id

    def version(self):
        return self.run('version')

    def start_server(self):
        output = self.run('start-server')
        self._print(output)

    def kill_server(self):
        output = self.run('kill-server')
        self._print(output)

    def await(self, target_device=None):
        output = self.run('wait-for-device', target_device=target_device)
        self._print(output)

    def reboot(self, target_device=None):
        output = self.run('reboot', target_device=target_device)
        self._print(output)

    def get_installed_packages(self, target_device=None):
        packages = []
        output = self.shell('pm list packages -f', target_device=target_device)
        for package in output.splitlines():
            parts = package.split('=')
            if len(parts) == 2:
                packages.append(parts[1].strip())
        return packages

    def get_package_activities(self, package_name, target_device=None):
        output = self.shell('dumpsys package ' + package_name)
        matches = re.finditer(r'\w{8} %s/([\.\w]+) filter \w{8}' % package_name, output)
        seen_activities = set()
        for mat in matches:
            activity = mat.group(1)
            if activity not in seen_activities and len(activity.split('.')) == 2:
                seen_activities.add(activity)
        return list(seen_activities)

    def get_window(self, target_device=None):
        ''' Returns the window that currently has focus '''
        curr_focus = self.shell('dumpsys window windows', grep='mCurrentFocus',
                                target_device=target_device)
        curr_app = self.shell('dumpsys window windows', grep='mFocusedApp',
                              target_device=target_device)
                              
        output = re.findall(r'[\w\.]+/[\w\.]+', curr_app)
        if len(output) == 0:
            raise RuntimeError('Current window focus could not be found in dumpsys')

        package_name, activity = output[0].split('/', 2)

        if 'Application Error' in curr_focus:
            raise SystemError('Application error')
        elif 'Application Not Responding' in curr_focus:
            raise TimeoutError('Application not responding')

        return package_name, activity
        
    def get_view(self, target_device=None):
        ''' Returns ElementTree object of the XML hierarchy of the current view '''
        xml_raw = self.shell('uiautomator dump /dev/tty')[:-len('UI hierchary dumped to: /dev/tty')]
        xml_parsed = ElementTree.fromstring(xml_raw)
        return xml_parsed
        
    def launch(self, package_name, activity=None, target_device=None):
        if activity:
            prefix = 'am start -n '
            suffix = ('/.' if activity[0] != '.' else '/') + activity
        else:
            prefix = 'monkey -p '
            suffix = ' -c android.intent.category.LAUNCHER 1'
            
        output = self.shell(prefix + package_name + suffix,
                            target_device=target_device)
                            
    def url(self, url, target_device=None):
        self.shell('am start -a android.intent.action.VIEW -d "%s"' % url,
                   target_device=target_device)

    def wakeup(self, target_device=None):
        if not self.pending_wakeup:
            self.pending_wakeup = True
            isawake = lambda: self.shell('dumpsys power', grep='mScreenOn=|Display Power: state=')
            
            output = isawake()
            if 'mScreenOn=false' in output or 'state=OFF' in output:
                self._print('Waking up screen by pressing power button...')
                self.press_key('power', target_device=target_device, wait=3)
                self._print('Unlocking screen by pressing menu button...')
                self.press_key('menu', target_device=target_device, wait=3)
                output = isawake()
            
            if 'mScreenOn=true' not in output and 'state=ON' not in output:
                raise RuntimeError('Wakeup failed or current screen state unknown')
            self.pending_wakeup = False

    def screenshot(self, target_device=None):
        try:
            from PIL import Image
        except ImportError:
            # https://github.com/scipy/scipy/blob/v0.16.0/scipy/ndimage/io.py#L8
            raise ImportError('Could not import the Python Imaging Library (PIL)'
                              ' required to load image files.  Please refer to'
                              ' http://pypi.python.org/pypi/PIL/ for installation'
                              ' instructions.')

        tmp = str(uuid.uuid4())
        self.wakeup(target_device=target_device)
        # adb exec-out screencap -p > test.png
        self.shell(['screencap', '/sdcard/%s.png' % tmp], target_device=target_device)
        self.run(['pull', '/sdcard/%s.png' % tmp, '%s.png' % tmp], target_device=target_device)
        self.shell(['rm', '/sdcard/%s.png' % tmp], target_device=target_device)
        img = Image.open('%s.png' % tmp)
        os.remove('%s.png' % tmp)
        return img

    def press_key(self, keynames, target_device=None, wait=0.5):
        keynames = [k.upper() for k in HostProcess.type_check_cmd(keynames)]
        if not all(k in __KEY_CODES__ for k in keynames):
            raise ValueError('Provided key %r does not have a mapping' % keynames)
        keycodes = ' '.join(['%d' % __KEY_CODES__[k] for k in keynames])
        self.wakeup(target_device=target_device)
        self.shell('input keyevent %s' % keycodes, target_device=target_device)
        time.sleep(wait)

    def input_text(self, text, target_device=None, wait=0.5):
        self.wakeup(target_device=target_device)
        self.shell('input text \'%s\'' % text.replace(' ', '%s'), target_device=target_device)
        time.sleep(wait)
        
    def install(self, apk_file, target_device=None, opts='r'):
        self.run('install %s %s' % (('-' + opts) if opts else '', apk_file),
                 target_device=target_device)

    def uninstall(self, package_name, target_device=None, opts=None):
        self.run('uninstall %s %s' % (('-' + opts) if opts else '', package_name),
                 target_device=target_device)
                 