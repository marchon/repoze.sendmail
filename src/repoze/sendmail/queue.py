import atexit
import errno
import logging
import os
import os.path
import smtplib
import stat
import sys
import threading
import time

from repoze.sendmail.maildir import Maildir
from repoze.sendmail.mailer import SMTPMailer

if sys.platform == 'win32':
    import win32file
    _os_link = lambda src, dst: win32file.CreateHardLink(dst, src, None)
else:
    _os_link = os.link
    
# The below diagram depicts the operations performed while sending a message.  
# This sequence of operations will be performed for each file in the maildir 
# on which ``send_message`` is called.
#
# Any error conditions not depected on the diagram will provoke the catch-all
# exception logging of the ``send_message`` method.
#
# In the diagram the "message file" is the file in the maildir's "cur" directory
# that contains the message and "tmp file" is a hard link to the message file
# created in the maildir's "tmp" directory.
#
#           ( start trying to deliver a message )
#                            |
#                            |
#                            V
#            +-----( get tmp file mtime )
#            |               |
#            |               | file exists
#            |               V
#            |         ( check age )-----------------------------+
#   tmp file |               |                       file is new |
#   does not |               | file is old                       |
#   exist    |               |                                   |
#            |      ( unlink tmp file )-----------------------+  |
#            |               |                      file does |  |
#            |               | file unlinked        not exist |  |
#            |               V                                |  |
#            +---->( touch message file )------------------+  |  |
#                            |                   file does |  |  |
#                            |                   not exist |  |  |
#                            V                             |  |  |
#            ( link message file to tmp file )----------+  |  |  |
#                            |                 tmp file |  |  |  |
#                            |           already exists |  |  |  |
#                            |                          |  |  |  |
#                            V                          V  V  V  V
#                     ( send message )             ( skip this message )
#                            |
#                            V
#                 ( unlink message file )---------+
#                            |                    |
#                            | file unlinked      | file no longer exists
#                            |                    |
#                            |  +-----------------+
#                            |  |
#                            |  V
#                  ( unlink tmp file )------------+
#                            |                    |
#                            | file unlinked      | file no longer exists
#                            V                    |
#                  ( message delivered )<---------+


# The longest time sending a file is expected to take.  Longer than this and
# the send attempt will be assumed to have failed.  This means that sending
# very large files or using very slow mail servers could result in duplicate
# messages sent.
MAX_SEND_TIME = 60*60*3

class QueueProcessor(object):
    log = logging.getLogger("QueueProcessor")

    __stopped = False
 
    maildir = None

    mailer = None

    def setQueuePath(self, path):
        self.maildir = Maildir(path, True)

    queue_path = property(None, setQueuePath)

    def __init__(self, mailer=None, queue_path=None, maildir=None):
        self.mailer = mailer
        self.maildir = maildir
        if queue_path:
            self.setQueuePath(queue_path)
            
    def send_messages_thread(self, interval=3.0):
        thread = threading.Thread(target=self.send_messages_daemon,
                                  name="repoze.sendmail.QueueProcessorThread")
        thread.start()

        # Python versions <2.6 don't respect name argument on constructor
        if not hasattr(thread, "name"):
            thread.name = "repoze.sendmail.QueueProcessorThread"
        thread.queue_processor = self

        return thread
    
    def send_messages_daemon(self, interval=3.0):
        atexit.register(self.stop)
        while not self.__stopped:
            self.send_messages()
            if not self.__stopped:
                time.sleep(interval)

    def send_messages(self):
        for filename in self.maildir:
            # if we are asked to stop while sending messages, do so
            if self.__stopped:
                break
            self._send_message(filename)
            
    def _parseMessage(self, message):
        """Extract fromaddr and toaddrs from the first two lines of
        the `message`.

        Returns a fromaddr string, a toaddrs tuple and the message
        string.
        """

        fromaddr = ""
        toaddrs = ()
        rest = ""

        try:
            first, second, rest = message.split('\n', 2)
        except ValueError:
            return fromaddr, toaddrs, message

        if first.startswith("X-Zope-From: "):
            i = len("X-Zope-From: ")
            fromaddr = first[i:]

        if second.startswith("X-Zope-To: "):
            i = len("X-Zope-To: ")
            toaddrs = tuple(second[i:].split(", "))

        return fromaddr, toaddrs, rest

    def _send_message(self, filename):
        fromaddr = ''
        toaddrs = ()
        head, tail = os.path.split(filename)
        tmp_filename = os.path.join(head, '.sending-' + tail)
        rejected_filename = os.path.join(head, '.rejected-' + tail)
        try:
            # perform a series of operations in an attempt to ensure
            # that no two threads/processes send this message
            # simultaneously as well as attempting to not generate
            # spurious failure messages in the log; a diagram that
            # represents these operations is included in a
            # comment above this class
            try:
                # find the age of the tmp file (if it exists)
                age = None
                mtime = os.stat(tmp_filename)[stat.ST_MTIME]
                age = time.time() - mtime
            except OSError, e:
                if e.errno == errno.ENOENT: # file does not exist
                    # the tmp file could not be stated because it
                    # doesn't exist, that's fine, keep going
                    pass
                else:
                    # the tmp file could not be stated for some reason
                    # other than not existing; we'll report the error
                    raise

            # if the tmp file exists, check it's age
            if age is not None:
                try:
                    if age > MAX_SEND_TIME:
                        # the tmp file is "too old"; this suggests
                        # that during an attemt to send it, the
                        # process died; remove the tmp file so we
                        # can try again
                        os.unlink(tmp_filename)
                    else:
                        # the tmp file is "new", so someone else may
                        # be sending this message, try again later
                        return
                    # if we get here, the file existed, but was too
                    # old, so it was unlinked
                except OSError, e:
                    if e.errno == errno.ENOENT: # file does not exist
                        # it looks like someone else removed the tmp
                        # file, that's fine, we'll try to deliver the
                        # message again later
                        return

            # now we know that the tmp file doesn't exist, we need to
            # "touch" the message before we create the tmp file so the
            # mtime will reflect the fact that the file is being
            # processed (there is a race here, but it's OK for two or
            # more processes to touch the file "simultaneously")
            try:
                os.utime(filename, None)
            except OSError, e:
                if e.errno == errno.ENOENT: # file does not exist
                    # someone removed the message before we could
                    # touch it, no need to complain, we'll just keep
                    # going
                    return
                
                else:
                    # Some other error, propogate it
                    raise
                
            # creating this hard link will fail if another process is
            # also sending this message
            try:
                _os_link(filename, tmp_filename)
            except OSError, e:
                if e.errno == errno.EEXIST: # file exists, *nix
                    # it looks like someone else is sending this
                    # message too; we'll try again later
                    return

                else:
                    # Some other error, propogate it
                    raise
                
            # FIXME: Need to test in Windows.  If 
            # test_concurrent_delivery passes, this stanza can be
            # deleted.  Otherwise we probably need to catch 
            # WindowsError and check for corresponding error code.
            #except error, e:
            #    if e[0] == 183 and e[1] == 'CreateHardLink':
            #        # file exists, win32
            #        return

            # read message file and send contents
            file = open(filename)
            message = file.read()
            file.close()
            fromaddr, toaddrs, message = self._parseMessage(message)
            try:
                self.mailer.send(fromaddr, toaddrs, message)
            except smtplib.SMTPResponseException, e:
                if 500 <= e.smtp_code <= 599:
                    # permanent error, ditch the message
                    self.log.error(
                        "Discarding email from %s to %s due to"
                        " a permanent error: %s",
                        fromaddr, ", ".join(toaddrs), str(e))
                    _os_link(filename, rejected_filename)
                else:
                    # Log an error and retry later
                    raise

            try:
                os.unlink(filename)
            except OSError, e:
                if e.errno == errno.ENOENT: # file does not exist
                    # someone else unlinked the file; oh well
                    pass
                else:
                    # something bad happend, log it
                    raise

            try:
                os.unlink(tmp_filename)
            except OSError, e:
                if e.errno == errno.ENOENT: # file does not exist
                    # someone else unlinked the file; oh well
                    pass
                else:
                    # something bad happened, log it
                    raise

            # TODO: maybe log the Message-Id of the message sent
            self.log.info("Mail from %s to %s sent.",
                          fromaddr, ", ".join(toaddrs))
        
        # Catch errors and log them here
        except:
            if fromaddr != '' or toaddrs != ():
                self.log.error(
                    "Error while sending mail from %s to %s.",
                    fromaddr, ", ".join(toaddrs), exc_info=True)
            else:
                self.log.error(
                    "Error while sending mail : %s ",
                    filename, exc_info=True)

    def stop(self):
        self.__stopped = True

class ConsoleApp(object):
    """Allows running of Queue Processor from the console.
    
    Currently this is hardcoded to use an SMTPMailer to deliver messages.  I am
    still contemplating what a better configuration story for this might be.
    
    """
    _usage = """%(script_name)s [OPTIONS] path/to/maildir
    
    OPTIONS:
        --daemon            Run in daemon mode, periodically checking queue 
                            and sending messages.  Default is to send all 
                            messages in queue once and exit.
                     
        --interval <#secs>  How often to check queue when in daemon mode.
                            Default is 3 seconds.
                               
        --hostname          Name of smtp host to use for delivery.  Default is
                            localhost.
                            
        --port              Which port on smtp server to deliver mail to.  
                            Default is 25.
                            
        --username          Username to use to log in to smtp server.  Default
                            is none.
                            
        --password          Password to use to log in to smtp server.  Must be
                            specified if username is specified.
                            
        --force-tls         Do not connect if TLS is not available.  Not 
                            enabled by default.
                            
        --no-tls            Do not use TLS even if is available.  Not enabled
                            by default.
    """
    _error = False
    daemon = False
    interval = 3
    hostname = "localhost"
    port = 25
    username = None
    password = None
    force_tls = False
    no_tls = False
    queue_path = None
    
    def __init__(self, argv=sys.argv):
        self.script_name = argv[0]
        self._process_args(argv[1:])
        
    def main(self):
        if self._error:
            return
        
        mailer = SMTPMailer(self.hostname,
                            self.port,
                            self.username,
                            self.password,
                            self.no_tls,
                            self.force_tls)
        qp = QueueProcessor(mailer, self.queue_path)
        if self.daemon:
            qp.send_messages_daemon()
        else:
            qp.send_messages()
        
    def _process_args(self, args):
        while args:
            arg = args.pop(0)
            if arg == "--daemon":
                self.daemon = True
                
            elif arg == "--interval":
                try:
                    self.interval = float(args.pop(0))
                except:
                    self._error_usage()

            elif arg == "--hostname":
                if not args:
                    self._error_usage()
                self.hostname = args.pop(0)
                
            elif arg == "--port":
                try:
                    self.port = int(args.pop(0))
                except:
                    self._error_usage()
                    
            elif arg == "--username":
                if not args:
                    self._error_usage()
                self.username = args.pop(0)

            elif arg == "--password":
                if not args:
                    self._error_usage()
                self.password = args.pop(0)
                
            elif arg == "--force-tls":
                self.force_tls = True
                
            elif arg == "--no-tls":
                self.no_tls = True
                
            elif arg.startswith("-") or self.queue_path:
                self._error_usage()
                
            else:
                self.queue_path = arg
        
        if not self.queue_path:
            self._error_usage()
            
        if (self.username or self.password 
            and not (self.username and self.password)):
            print >>sys.stderr, "Must use username and password together."
            self._error = True
            
    def _error_usage(self):
        print >>sys.stderr, self._usage % {"script_name": self.script_name,}
        self._error = True
        
def run_console():
    app = ConsoleApp()
    app.main()
    
if __name__ == "__main__":
    run_console()
    