[![test_ubuntu](https://github.com/kirxkirx/unmw/actions/workflows/test_ubuntu.yml/badge.svg)](https://github.com/kirxkirx/unmw/actions/workflows/test_ubuntu.yml)

# unmw
This repository contains server-side scripts for transient search with [VaST](https://github.com/kirxkirx/vast).
These are the scripts used to analyze the [NMW survey](https://scan.sai.msu.ru/nmw/) images.

This is very much work in progress. You may reach me at kirx[at]kirx.net

# draft installation instructions

 1. place the code in 'cgi-bin'
````
cd /var/www/scan.sai.msu.ru/cgi-bin
git clone https://github.com/kirxkirx/unmw.git
````
 2. create the data directory
````
mkdir /home/NMW_web_upload
````
 3. create a symlink (or use `mount --bind` if your Apache configuration does
not allow symlinks) to the data directory
````
cd /var/www/scan.sai.msu.ru/cgi-bin/unmw
ln -s /home/NMW_web_upload uploads
````
 4. copy the content of 'move_to_htdocs' folder to your htdocs
````
cp -r move_to_htdocs /var/www/scan.sai.msu.ru/htdocs/unmw
````
 5. create a symlink (or use `mount --bind`) to the data directory in htdocs
````
cd /var/www/scan.sai.msu.ru/htdocs/unmw
ln -s /home/NMW_web_upload uploads
````
 6. go back to the cgi directory and set the data directory path and the URL exposing it in 
'local_config.sh', see the example in 'local_config.sh_example'

 7. Add `unmw/combine_reports.sh` to cron for it to be run every few minutes. For example `/etc/crontab` may look like:
 ````
*/8     *       *       *       *       www-data        /dataX/cgi-bin/unmw/combine_reports.sh &> /dev/null
 ````
where www-data is the apache user, `/dataX/cgi-bin/unmw/combine_reports.sh` is the full path to `combine_reports.sh` (will be different for your system).

# An overly-detailed and ugly example installation on a fresh AlmaLinux 9
````
# The following commands should be executed as root

# (optional) Update the system
dnf update -y

# Install packages needed for VaST
dnf install libX11-devel
dnf install libpng-devel
dnf install gfortran

# Install packages needed for VaST control scripts
dnf install zip
# !!! Install rar archiver manually !!!
# !!! DO NOT USE unrar - it is incompatible with rar version at the NMW camera control computers
# unrar will produce corrupted files after unpacking. !!!
# The linux binary rar may be found at https://www.win-rar.com/download.html

# (optional) Install sensors for CPU temperature monitoring
dnf install sensors
sensors-detect

# Install Apache web server
dnf install httpd -y
systemctl enable httpd
systemctl start httpd

# Create directory for data, cgi-scripts and the analysis code
mkdir -p /data/cgi-bin
chown apache:apache /data/cgi-bin
chmod 755 /data/cgi-bin

# Use text editor to change the CGI directory from default /var/www/cgi-bin/ to /data/cgi-bin/
# in the Apache configuration file 
# (you'll need to update ScriptAlias and the relevant Directory section, 
# make sure the section includes "Options +ExecCGI")
nano /etc/httpd/conf/httpd.conf

# Check Apache configuration file syntax
apachectl configtest

# If the test is successful, restart Apache
systemctl restart httpd

#### At this point, regret that we are doing this on a paranoid RedHat derivative

# Configure the firewall to allow HTTP traffic
firewall-cmd --permanent --add-service=http
firewall-cmd --reload

# Check if SElinux is enabled
getenforce

# if Enforcing, configure SElinux to allow CGI scripts
#semanage fcontext -a -t httpd_sys_script_exec_t "/data/cgi-bin(/.*)?"
#restorecon -R /data/cgi-bin
#
# The overall transient search systems in its current form seems incompatible with SElinux.
# !!! Disable SElinux permanently !!!
grubby --update-kernel ALL --args selinux=0
# then reboot reboot

#### End of Linux distribution related regrets

# Install the wrapper scripts in cgi-bin
cd /data/cgi-bin
git clone https://github.com/kirxkirx/unmw.git

# Create the data directory
mkdir /data/cgi-bin/unmw/uploads
chown apache:apache /data/cgi-bin/unmw/uploads

# Make a directory for static pages
mkdir /var/www/html/unmw
chown apache:apache /var/www/html/unmw

# Create a symlink (or use mount --bind) to the data directory in the static pages directory
ln -s /data/cgi-bin/unmw/uploads /var/www/html/unmw/uploads

# Create and edit the local configuration file for the control scripts
# IMAGE_DATA_ROOT and DATA_PROCESSING_ROOT should be set to /data/cgi-bin/unmw/uploads
# while URL_OF_DATA_PROCESSING_ROOT should be set to 
# the URL at which the content of /data/cgi-bin/unmw/uploads is accessible
# You may also need to set mkdir /data/reference_images
cp /data/cgi-bin/unmw/local_config.sh_example /data/cgi-bin/unmw/local_config.sh
nano /data/cgi-bin/unmw/local_config.sh
chmod +x /data/cgi-bin/unmw/local_config.sh

# Install VaST to where the control scripts will find it
cd /data/cgi-bin/unmw/uploads/
sudo -u apache git clone https://github.com/kirxkirx/vast/
cd /data/cgi-bin/unmw/uploads/vast
sudo -u apache make
sudo -u apache lib/update_offline_catalogs.sh force

# Create reference images directory
mkdir /data/reference_images
# !!! copy the reference images to that directory !!!

# Add summary transient report creation and offline catalog updating to crontab.
# It may look something like
# */8     *       *       *       *       apache  /data/cgi-bin/unmw/combine_reports.sh &> /data/cgi-bin/unmw/uploads/combine_reports_cronlog.txt
# 00      16      *       *       2       apache  /data/cgi-bin/unmw/uploads/vast/lib/update_offline_catalogs.sh force > /data/cgi-bin/unmw/uploads/vast/lib/catalogs/update_offline_catalogs.log
nano /etc/crontab



# (optional, but recommended) Install local copy of astrometry.net code
# the following command are executed as user with sudo for operations requiring root
sudo dnf install bzip2-devel cfitsio-devel libjpeg-turbo-devel cairo-devel numpy python3-devel
# The swig package was not tin the available repositories, so had to enable a new one
# that is called AlmaLinux 9 - CRB
sudo dnf config-manager --set-enabled crb
sudo dnf install swig
# There is no astropy package for Alma Linux 9, so let's use pip
sudo pip3 install astropy
# Test that astropy works
python3 -c "import astropy; print(astropy.__version__)"
# Now get and compile the actual astrometry.net code
git clone https://github.com/dstndstn/astrometry.net.git
cd astrometry.net
./configure && make
sudo make install
# !!! Download the index files from http://data.astrometry.net/ !!!
# !!! and copy them to /usr/local/astrometry/data/ !!!

# (optional) use VaST test data to check that VaST found the local astrometry.net
util/wcs_image_calibration.sh ../NMW-STL__find_Neptune_test/second_epoch_images/000_2023-7-19_21-26-29_003.fts
util/listhead wcs_000_2023-7-19_21-26-29_003.fts | grep VAST
# The second command should print something like 
# """"
# VAST001 = 'wcs_image_calibration.sh' / VaST script name
# VAST002 = 'local   '           / ASTROMETRYNET_LOCAL_OR_REMOTE
# VAST003 = 'local   '           / PLATE_SOLVE_SERVER
# VAST004 = 'iteration02'        / astrometry.net run
# """"
# Indicating that the local copy of astrometry.net code was used, not a remote one

# (optional) Install swarp and ImageMagick for the fastplot script
# swarp
git clone https://github.com/astromatic/swarp.git
cd swarp/
./autogen.sh
./configure && make
sudo make install
# ImageMagick
dnf install ImageMagick

# Install beautifulsoup4 for preparing filtered combined reports (that exclude known things)
dnf install python3-beautifulsoup4

# Final touches
#
# Create the candidate exclusion list for the camera
touch /data/cgi-bin/unmw/uploads/exclusion_list_STL.txt
chown apache:apache /data/cgi-bin/unmw/uploads/exclusion_list_STL.txt

````
# Alternatively
Have a look at the [testing script](unmw_selftest.sh) that spins-up a python built-in [HTTP server](custom_http_server.py) at port 8080 (or the next one available) and puts a copy of [VaST](https://github.com/kirxkirx/vast) and all the uploaded images and processing results in the `uploads` subdirectory of the current directory.
The testing script relies on external services for plate solving and accessing
the [UCAC5](https://cdsarc.cds.unistra.fr/viz-bin/Cat?I/340) catalog, which is fine for testing but might be too slow or
unreliable for production. After initial testing, users are encouraged to
install local copies of [UCAC5](https://cdsarc.cds.unistra.fr/viz-bin/Cat?I/340)
(you may place its files at `uploads/ucac5` or `$HOME/ucac5`) and [astrometry.net code](https://github.com/dstndstn/astrometry.net) (and its associated [index files](http://data.astrometry.net/)).
