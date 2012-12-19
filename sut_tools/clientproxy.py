#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

#
# Assumes Python 2.6
#

import os, sys
import time
import pwd
import atexit
import signal
import socket
import asyncore
import logging
import datetime
import traceback
import subprocess

from optparse import OptionParser
from Queue import Empty
from logging.handlers import RotatingFileHandler
from multiprocessing import Process, Queue, current_process, get_logger, log_to_stderr

from sut_lib import checkSlaveAlive, checkSlaveActive, stopSlave, getOurIP, getIPAddress, \
                    dumpException, runCommand, loadOptions, getLastLine, setFlag


"""clientproxy.py

Manage a buildbot client/slave instance for each defined Android device.

    Parameters
        --bbpath   <path>   Parent directory where the buildslave to control is located.
        --device            Device to manage. If not given it will be determined.
        --deviceIP          IP address of Device to manage. Will be discovered if not given.
        --debug             Turn on debug logging.
        --background        Fork to a daemon process.
        --logpath           Path where log file output is written.
        --pidpath           Path where PID file is written.
        --hangtime          Timeout value in seconds before a slave can be marked as hung.
"""


log             = get_logger()
eventQueue      = Queue()
options         = None
daemon          = None

sutDataPort     = 20700
sutDialbackPort = 42000 # base - device id # will be added to this
maxErrors       = 5

ourIP           = None
ourPath         = os.getcwd()
ourName         = os.path.splitext(os.path.basename(sys.argv[0]))[0]


defaultOptions = {
                   'debug':      ('-d', '--debug',      False,    'Enable Debug', 'b'),
                   'background': ('-b', '--background', False,    'daemonize ourselves', 'b'),
                   'bbpath':     ('-p', '--bbpath',     ourPath,  'Path where the Device buildbot slave clients can be found'),
                   'device':     ('',   '--device',     None,     'Device to manage, if not given it will be figured out from environment'),
                   'deviceIP':   ('',   '--deviceIP',   None,     'IP of Device to manage, if not given it will found via nslookup'),
                   'logpath':    ('-l', '--logpath',    ourPath,  'Path where log file is to be written'),
                   'pidpath':    ('',   '--pidpath',    ourPath,  'Path where the pid file is to be created'),
                   'hangtime':   ('',   '--hangtime',   1200,     'How long (in seconds) a slave can be idle'),
                 }


def initLogs(options):
    fileHandler   = RotatingFileHandler(os.path.join(options.logpath, '%s.log' % ourName), maxBytes=1000000, backupCount=99)
    fileFormatter = logging.Formatter('%(asctime)s %(levelname)-7s %(processName)s: %(message)s')

    fileHandler.setFormatter(fileFormatter)

    log.addHandler(fileHandler)
    log.fileHandler = fileHandler

    if not options.background:
        echoHandler   = logging.StreamHandler()
        echoFormatter = logging.Formatter('%(levelname)-7s %(processName)s: %(message)s')

        echoHandler.setFormatter(echoFormatter)

        log.addHandler(echoHandler)
        log.info('echoing')

    if options.debug:
        log.setLevel(logging.DEBUG)
        log.info('debug level is on')
    else:
        log.setLevel(logging.INFO)

    # Make sure sut_lib can log to our file as well
    import sut_lib
    sut_lib.log = log

class Daemon(object):
    def __init__(self, pidfile):
        self.stdin   = '/dev/null'
        self.stdout  = '/dev/null'
        self.stderr  = '/dev/null'
        self.pidfile = pidfile

    def handlesigterm(self, signum, frame):
        if self.pidfile is not None:
            try:
                eventQueue.put(('terminate',))
                os.remove(self.pidfile)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                pass
        sys.exit(0)

    def start(self):
        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError, exc:
            sys.stderr.write("%s: failed to fork from parent: (%d) %s\n" % (sys.argv[0], exc.errno, exc.strerror))
            sys.exit(1)

        os.chdir("/")
        os.setsid()
        os.umask(0)

        try:
            pid = os.fork()
            if pid > 0:
                sys.stdout.close()
                sys.exit(0)
        except OSError, exc:
            sys.stderr.write("%s: failed to fork from parent #2: (%d) %s\n" % (sys.argv[0], exc.errno, exc.strerror))
            sys.exit(1)

        sys.stdout.flush()
        sys.stderr.flush()

        si = open(self.stdin, "r")
        so = open(self.stdout, "a+")
        se = open(self.stderr, "a+", 0)

        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())

        if self.pidfile is not None:
            open(self.pidfile, "wb").write(str(os.getpid()))

        signal.signal(signal.SIGTERM, self.handlesigterm)

    def stop(self):
        if self.pidfile is None:
            sys.exit("no pidfile specified")
        try:
            pidfile = open(self.pidfile, "rb")
        except IOError, exc:
            sys.exit("can't open pidfile %s: %s" % (self.pidfile, str(exc)))
        data = pidfile.read()
        try:
            pid = int(data)
        except ValueError:
            sys.exit("mangled pidfile %s: %r" % (self.pidfile, data))
        os.kill(pid, signal.SIGTERM)

class DialbackHandler(asyncore.dispatcher_with_send):
    def __init__(self, sock, events):
        asyncore.dispatcher_with_send.__init__(self, sock)

        self.events = events

    def handle_read(self):
        data = self.recv(1024)
        log.info('data: %s' % data)

        if data.startswith('register '):
            self.send('OK\n')
            ip  = self.addr[0].strip()
            self.events.put(('dialback', ip))

class DialbackServer(asyncore.dispatcher):
    def __init__(self, host, port, events):
        asyncore.dispatcher.__init__(self)

        self.events = events
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        log.info('Binding to %s port %d' % (host, port))
        self.bind((host, port))
        self.listen(5)

    def handle_accept(self):
        log.info('incoming request')
        pair = self.accept()
        if pair is not None:
            sock, addr = pair
            log.info('Connection from %s' % repr(addr))
            handle = DialbackHandler(sock, self.events)

def handleDialback(port, events):
    """Dialback listener
    The SUTAgent will 'ping' our address/port when it first starts.
    
    This DialbackHandler method will be called for an incoming Device's ping
    when the listener is connected to by SUTAgent.
    
    NOTE: This code should only be run in it's own thread/process
          as it will not return
    """
    dialback = DialbackServer('0.0.0.0', port, events)
    while asyncore.socket_map:
        try:
            asyncore.loop(timeout=1, count=1)
        except:
            dumpException('error during dialback loop')
            break

def sendReboot(ip, port):
    log.warning('sending rebt to device')
    try:
        hbSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        hbSocket.settimeout(float(120))
        hbSocket.connect((ip, port))
        hbSocket.send('rebt\n')
        hbSocket.recv(4096)
    except:
        log.debug('Error sending reboot')

def monitorEvents(options, events):
    """This is the main state machine for the Device monitor.

    Respond to the events sent via queue and also monitor the
    state of the buildslave if it's been started.
    """
    pidFile   = os.path.join(options.bbpath, 'twistd.pid')
    errorFile = os.path.join(options.bbpath, 'error.flg')
    bbEnv     = { 'PATH':     os.getenv('PATH'),
                  'HOME':     os.getenv('HOME'),
                  'SUT_NAME': options.device,
                  'SUT_IP':   options.deviceIP,
                }

    event         = None
    bbActive      = False
    deviceActive   = False
    connected     = False
    nChatty       = 0
    maxChatty     = 10
    hbFails       = 0
    maxFails      = 50
    sleepFails    = 5
    softCount     = 0    # how many times deviceActive is True
                         # but errorFlag is set
    softCountMax  = 5    # how many active events to wait bdfore
                         # triggering a soft reset
    softResets    = 0
    softResetMax  = 5    # how many soft resets do we try before
                         # waiting for a hard reset
    hardResets    = 0
    hardResetsMax = 3
    lastHangCheck = time.time()
    lastNamedEvent = None # Used to determine what our last event was

    log.info('monitoring started (process pid %s)' % current_process().pid)

    while True:
        if not connected:
            try:
                hbSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                hbSocket.settimeout(float(120))
                hbSocket.connect((options.deviceIP, sutDataPort))
                connected = True
            except:
                connected = False
                hbFails  += 1
                log.info('Error connecting to data port - sleeping for %d seconds' % sleepFails)
                time.sleep(sleepFails)

        try:
            event = events.get(False)
        except Empty:
            event = None

        log.debug("EVENT: %s" % event)
        if event is None:
            if connected:
                try:
                    hbData = hbSocket.recv(4096)
                except:
                    hbData    = ''
                    connected = False
                    dumpException('hbSocket.recv()')

                if len(hbData) > 1:
                    log.debug('socket data [%s]' % hbData[:-1])

                    if 'ebooting ...' in hbData:
                        log.warning('device is rebooting')
                        events.put(('reboot',))
                        hbSocket.close()
                        connected = False
                    else:
                        log.info('heartbeat detected')
                        events.put(('active',))
                        hbFails = 0
                else:
                    hbFails += 1
        else:
            state = event[0]
            s     = 'event %s hbFails %d / %d' % (state, hbFails, maxFails)
            nChatty += 1
            if nChatty > maxChatty:
                log.info(s)
            else:
                log.debug(s)
            if nChatty > maxChatty:
                nChatty = 0

            if state == 'reboot':
                deviceActive = False
            elif state == 'stop' or state == 'offline':
                stopSlave(pidFile)
                bbActive = False
                if connected and state == 'offline':
                    hbSocket.close()
                    connected = False
            elif state == 'active' or state == 'dialback':
                deviceActive = True
                if not bbActive:
                    if os.path.isfile(errorFile):
                        log.warning('Device active but error flag set [%d/%d]' % (softCount, softResets))
                        softCount += 1
                        if softCount > softCountMax:
                            softCount = 0
                            if softResets < softResetMax:
                                softResets += 1
                                log.warning('removing error flag to see if device comes back')
                                os.remove(errorFile)
                            else:
                                hardResets += 1
                                log.warning('hard reset reboot check [%d/%d]' % (hardResets, hardResetsMax))
                                if hardResets < hardResetsMax:
                                    sendReboot(options.deviceIP, sutDataPort)
                                else:
                                    events.put(('offline',))
                    else:
                        events.put(('verify',))
            elif state == 'verify':
                if lastNamedEvent in ("verify", "start"):
                    continue # race got us recursed, skip back to top
                log.info('Running verify code')
                proc, output = runCommand(['python', '/builds/sut_tools/verify.py',
                                           options.device], env=bbEnv)
                if proc.returncode == 0:
                    for i in output:
                        log.debug(i)
                    log.info('verify.py has run without issues')
                    events.put(('start',))
                else:
                    for i in output:
                        log.warning(i)
                    log.warning('verify.py returned with errors')
                    if not os.path.isfile(errorFile):
                        log.warning('verify.py did not create \'%s\' as expected, bailing.' % errorFile)
                        setFlag(errorFile, "Remote Device Error: Verify.py returned with an unexpected error.")
                    # We fall back to normal errorFile handling, which should reboot and try to verify
                    # A few times before giving up
            elif state == 'start':
                if deviceActive and not bbActive:
                    log.debug('starting buildslave in %s' % options.bbpath)
                    bbProc, output = runCommand(['twistd', '--no_save',
                                                      '--rundir=%s' % options.bbpath,
                                                      '--pidfile=%s' % pidFile,
                                                      '--python=%s' % os.path.join(options.bbpath, 'buildbot.tac')], 
                                           env=bbEnv)
                    log.info('buildslave start returned %s' % bbProc.returncode)
                    if bbProc.returncode == 0 or bbProc.returncode == 1:
                        # pause to give twistd a chance to generate the pidfile
                        # before the code that follows goes off killing it because
                        # it thinks that it didn't start properly
                        # OMGRACECONDITIONWTF
                        nTries = 0
                        while nTries < 20:
                            nTries += 1
                            if os.path.isfile(pidFile):
                                log.debug('pidfile found, setting bbActive to True')
                                bbActive = True
                                break
                            else:
                                time.sleep(5)
                        # If we made it this far, we don't see a buildbot pidfile
                        log.warning("No buildbot pidfile found, as expected...")
                        log.info("See Output of buildslave start:")
                        log.info(output)
            elif state == 'dialback':
                softCount  = 0
                softResets = 0
                hardResets = 0
            elif state == 'terminate':
                break

            # Make sure to set this after the above state checks
            lastNamedEvent = state

        if hbFails > maxFails:
            hbFails     = 0
            sleepFails += 5
            if sleepFails > 300:
                sleepFails = 300
            events.put(('offline',))
            if connected:
                hbSocket.close()
                connected = False

        log.debug('bbActive %s deviceActive %s' % (bbActive, deviceActive))

        if os.path.isfile(errorFile):
            if bbActive:
                log.error('errorFile detected - sending stop request')
                events.put(('stop',))

        if bbActive:
            if os.path.isfile(pidFile):
                n = time.time()
                if not checkSlaveAlive(options.bbpath):
                    log.warning('buildslave should be active but pid is not alive')
                    if int(n - lastHangCheck) > 300:
                        lastHangCheck = n
                        logTD = checkSlaveActive(options.bbpath)
                        if logTD.days > 0 or (logTD.days == 0 and logTD.seconds > options.hangtime):
                            log.error('last activity was %d days %d seconds ago - marking as hung slave' % 
                                      (logTD.days, logTD.seconds))
                            events.put(('offline',))
            else:
                log.warning('buildslave should be active but pidfile not found, marking as offline')
                events.put(('offline',))
        else:
            if os.path.isfile(pidFile):
                if checkSlaveAlive(options.bbpath):
                    log.error('buildslave should NOT be active but pidfile found, killing buildbot')
                    events.put(('stop',))
                else:
                    log.warning('buildslave not active but pidfile found, removing pidfile')
                    os.remove(pidFile)

    if bbActive:
        stopSlave(pidFile)

    log.info('monitor stopped')

def handleSigTERM(signum, frame):
    db.close()
    if pidFile is not None:
        try:
            eventQueue.put(('terminate',))
            os.remove(pidFile)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            pass
    sys.exit(0)

def shutdownDialback():
    db.close()

if __name__ == '__main__':
    ourIP = getOurIP()

    if ourIP is None:
        log.error('our IP address is not defined, exiting')
        sys.exit(1)

    options = loadOptions(defaultOptions)
    initLogs(options)

    options.bbpath = os.path.abspath(options.bbpath)
    if options.device is None:
        if 'tegra-' in ourPath.lower() or 'panda-' in ourPath.lower():
            options.device = os.path.basename(os.path.split(ourPath)[-1])

    if options.device is None:
        log.error('Device has not been specified, exiting')
        sys.exit(1)

    if options.deviceIP is None:
        options.deviceIP = getIPAddress(options.device)

    try:
        n = int(options.device.split('-')[1])
    except:
        n = 0
    sutDialbackPort += n

    p = os.getpid()
    log.info('%s: ourIP %s device %s deviceIP %s bbpath %s' % (p, ourIP, options.device, options.deviceIP, options.bbpath))

    pidFile = os.path.join(ourPath, '%s.pid' % ourName)

    if options.background and not 'stop' in options.args:
        daemon = Daemon(pidFile)
        daemon.start()

    db = Process(name='dialback', target=handleDialback, args=(sutDialbackPort, eventQueue))

    signal.signal(signal.SIGTERM, handleSigTERM)
    atexit.register(shutdownDialback, db)

    db.start()
    monitorEvents(options, eventQueue)

