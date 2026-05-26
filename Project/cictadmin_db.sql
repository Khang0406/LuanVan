INSERT INTO websites (name, url, server_id, service_name, description)
SELECT 'Web Quan Ly', 'ql.local', id, 'nginx', 'Trang quan ly noi bo'
FROM servers
LIMIT 1;

INSERT INTO websites (name, url, server_id, service_name, description)
SELECT 'Web Cong Khai', 'congkhai.com', id, 'apache2', 'Trang web cong khai'
FROM servers
LIMIT 1;
