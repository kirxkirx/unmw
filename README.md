# unmw
server-side scripts used to run VaST on NMW survey images

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
 3. create a symlink (or use `mount --bind` if you Apache configuration does
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

# An overly-detailed example installation on a fresh AlmaLinux 9
````
# The following commands should be executed as root

# (optional) Update the system
dnf update -y

# Install packages needed for VaST
dnf install libX11-devel
dnf install libpng-devel
dnf install gfortran

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
semanage fcontext -a -t httpd_sys_script_exec_t "/data/cgi-bin(/.*)?"
restorecon -R /data/cgi-bin

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

# Install VaST to where the control scrpts will find it
cd /data/cgi-bin/unmw/uploads/
sudo -u apache git clone https://github.com/kirxkirx/vast/
cd /data/cgi-bin/unmw/uploads/vast
sudo -u apache make
sudo -u apache lib/update_offline_catalogs.sh force

# Create reference images directory
mkdir /data/reference_images
# !!! copy the reference images to that directory !!!

# Add summary transient report creation and offline catalog updating to corntab.
# It may look something like
# */8     *       *       *       *       apache  /data/cgi-bin/unmw/combine_reports.sh &> /data/cgi-bin/unmw/uploads/combine_reports_cronlog.txt
00      16      *       *       2       apache  /data/cgi-bin/unmw/uploads/vast/lib/update_offline_catalogs.sh force > /data/cgi-bin/unmw/uploads/vast/lib/catalogs/update_offline_catalogs.log

nano /etc/crontab

````