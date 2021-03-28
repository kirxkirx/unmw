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
