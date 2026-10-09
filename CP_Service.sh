cp ./Rclone_Sync_And_Check.service /etc/systemd/system/Rclone_Sync_And_Check.service
systemctl daemon-reload
systemctl enable Rclone_Sync_And_Check.service
systemctl start Rclone_Sync_And_Check.service