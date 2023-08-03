#!/usr/bin/env python2
import cgi, os
import cgitb; cgitb.enable()

# For JobID generation
import random
import string

# for sleep
import time
# for sys
import sys 
# for Popen
#import subprocess

# for socket.getfqdn()
import socket

#try: # Windows needs stdio set for binary mode.
#    import msvcrt
#    msvcrt.setmode (0, os.O_BINARY) # stdin  = 0
#    msvcrt.setmode (1, os.O_BINARY) # stdout = 1
#except ImportError:
#    pass

# Start log
message = 'Starting program ' + sys.argv[0] + ' <br>'


#### This is backup - assume the actual high-load handling will be done by the child script
# Check the system load
### These load values are very optiistic and rely on the autoprocess script to handle load balancing
### The idea is that we want to download the data now at all cost and then wait for the system load to get reasonably low
emergency_load = 55.0
# The commented-out stuff below is a terrible idea that results in timeout and multiple attempts to upload the smae files
# Just check the load and if it's not extreme - accept the data
if True == os.access('/proc/loadavg',os.R_OK):
 procload = open('/proc/loadavg','r')
 loadline = procload.readline()
 procload.close()
 load = float(loadline.split()[1])
 if load > emergency_load :
  message = message + 'System load is extremely high'
  sys.exit(1) # Just quit
#max_load = 55.0
#load = 99.0
#while load > max_load :
# load = 0.0
# if True == os.access('/proc/loadavg',os.R_OK):
#  procload = open('/proc/loadavg','r')
#  loadline = procload.readline()
#  procload.close()
#  load = float(loadline.split()[1])
#  if load > emergency_load :
#   message = message + 'System load is extremely high'
#   sys.exit(1) # Just quit
#  if load > max_load :
#   random.seed() # initialize using current system time, just in case...
#   sleep_time = 120*random.random()  
#   message = message + 'System load is too high: ' + str(load) + ', sleeping for ' + str(sleep_time) + ' seconds! <br> '
#   time.sleep(sleep_time)
             

form = cgi.FieldStorage()

# Get input parameters 
fileupload = 'True'

if fileupload == "True" :
 message = message + 'Uloading new file <br>'
 # Generator to buffer file chunks
 #def fbuffer(f, chunk_size=10000):
 def fbuffer(f, chunk_size=10000000):
    while True:
       chunk = f.read(chunk_size)
       if not chunk: break
       yield chunk
       
 # A nested FieldStorage instance holds the file
 fileitem = form['file']
 # Test if the file was NOT uploaded
# if not fileitem.filename:
#  message = 'ERROR!!! No file was uploaded. :('
#  print """\
#Content-Type: text/html\n
#<html><body>
#<p>%s</p>
#</body></html>
#""" % (message,)
#  sys.exit(0) # Just quit
#else:
# message = message + 'Non-interactive mode <br>'   

pid = os.getpid();
message = message + 'Process ID:  ' + str(pid) + ' <br>'

if fileupload == "True" :
 JobID = 'web_upload_' + str(pid)
 #random.seed() # initialize using current system time, just in case...
 for i in range(8):
  JobID = JobID + random.choice(string.letters)
  

dirname = 'uploads/' + JobID
if fileupload == "True" :
 os.mkdir(dirname)
dirname = dirname + '/'

# NEW
cgitb.enable(display=1, logdir=dirname)

if fileupload == "True" :
 # strip leading path from file name to avoid directory traversal attacks
 fn = os.path.basename(fileitem.filename)
 ####
 # truncate filename at 256 characters just in case
 fn = fn[:256]
 # some more tricks trying to sanitize the file name
 fn = fn.replace( ' ', '_')
 fn = fn.replace( '..', '_')
 fn = fn.replace( '%', '_')
 ####
 #f = open(dirname + fn, 'wb', 10000)
 f = open(dirname + fn, 'wb', 100000000)

 # Read the file in chunks
 for chunk in fbuffer(fileitem.file):
  f.write(chunk)
 f.close()
 message = message + 'The file "' + fn + '" was uploaded successfully! <br>'

fullhostname=socket.getfqdn()
message = message + '<br><br>The output will be written to <a href=\"http://' + fullhostname + '/nmw/proc/upload/' + dirname + '\">http://' + fullhostname + '/nmw/proc/upload/' + dirname + '</a><br><br>'


if form.getvalue('workstartemail') :
 syscmd = 'touch ' + dirname + 'workstartemail'
 CmdReturnStatus = os.system(syscmd)
 message = message + ' ' + syscmd + ' '

if form.getvalue('workendemail') :
 syscmd = 'touch ' + dirname + 'workendemail'
 CmdReturnStatus = os.system(syscmd)
 message = message + ' ' + syscmd + ' '

if form.getvalue('nonexistingfield') :
 syscmd = 'touch ' + dirname + 'nonexistingfield'
 CmdReturnStatus = os.system(syscmd)
 message = message + ' ' + syscmd + ' '

syscmd = 'ls -lh ' + dirname + fn + ' > ' + dirname + 'upload.log'
CmdReturnStatus = os.system(syscmd)

results_page_url = 'http://' + fullhostname + '/unmw/' + dirname


# Run the actual command
#syscmd = 'echo \"' + results_page_url + fn + '\" | mail -s \"new NMW images uplaoded" kirx@scan.sai.msu.ru'
#CmdReturnStatus = os.system(syscmd)   
#message = message + 'Command return status:  ' + str(CmdReturnStatus) + ' <br>'

# Run the actual command
#syscmd = './autoprocess.sh ' + dirname + fn + ' &>' + dirname + 'program.log'
syscmd = './wrapper.sh ' + dirname + fn
CmdReturnStatus = os.system(syscmd)
#CmdReturnStatus = subprocess.Popen(syscmd)
#CmdReturnStatus = subprocess.Popen( './autoprocess.sh', dirname + fn)

time.sleep(10)

# if the file does not exist yet, wait more
if os.path.isfile( dirname + "results_url.txt") != True :
 time.sleep(30)

if os.path.isfile( dirname + "results_url.txt") != True :
 time.sleep(30)

if os.path.isfile( dirname + "results_url.txt") != True :
 time.sleep(30)

if os.path.isfile( dirname + "results_url.txt") != True :
 time.sleep(60)

# give it a few more seconds to draft the actual HTML page at this URL
time.sleep(10)

# Open the silly file with the redirection link only if it exist
if os.path.isfile( dirname + "results_url.txt") == True :
 file1 = open( dirname + "results_url.txt","r")
 results_page_url = file1.readline()
 file1.close()
# otherwise fall back to the upload directory link

# Everything is fine - redirect
print """\
Content-Type: text/html\n
<html>
<head>
<meta http-equiv=\"Refresh\" content=\"0; url=%s\"> 
</head>
<body>
<p>%s</p>   
</body></html>
""" % (results_page_url,message,)

sys.exit(0) # Just quit
