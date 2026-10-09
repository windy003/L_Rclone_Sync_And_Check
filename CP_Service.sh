cp ./Check_Rclone_Dir.service /etc/systemd/system/Check_Rclone_Dir.service
systemctl daemon-reload
systemctl enable Check_Rclone_Dir.service
systemctl start Check_Rclone_Dir.service