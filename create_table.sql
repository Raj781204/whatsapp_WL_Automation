CREATE DATABASE IF NOT EXISTS whatsapp_db;
USE whatsapp_db;

CREATE TABLE IF NOT EXISTS server_details (
    id     INT AUTO_INCREMENT PRIMARY KEY,
    IP     VARCHAR(45),
    Port   INT,
    status VARCHAR(20)
);

-- Optional: clear old rows so ids start again from 1 (newest message = id 1)
-- TRUNCATE TABLE whatsapp_db.server_details;
