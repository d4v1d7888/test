#!/bin/bash
# setup_https.sh
# Configura HTTPS con certificado autofirmado en nginx
# para el Prometheus Target Manager.
#
# Uso: sudo bash setup_https.sh
# ──────────────────────────────────────────────────────────────────
 
set -e
 
IP=$(hostname -I | awk '{print $1}')
CERT_DIR="/etc/ssl/prometheus-target-manager"
NGINX_CONF="/etc/nginx/conf.d/prometheus-targets.conf"
DAYS=3650  # 10 años
 
echo "=== Prometheus Target Manager — Configuración HTTPS ==="
echo "IP del servidor: $IP"
echo ""
 
# ── 1. Crear directorio para los certificados ─────────────────────
echo "[1/4] Creando directorio de certificados..."
mkdir -p "$CERT_DIR"
 
# ── 2. Generar certificado autofirmado ────────────────────────────
echo "[2/4] Generando certificado autofirmado ($DAYS días)..."
openssl req -x509 -nodes -days $DAYS \
  -newkey rsa:2048 \
  -keyout "$CERT_DIR/server.key" \
  -out    "$CERT_DIR/server.crt" \
  -subj "/C=ES/ST=Madrid/L=Madrid/O=Monitoring/CN=$IP" \
  -addext "subjectAltName=IP:$IP"
 
chmod 600 "$CERT_DIR/server.key"
chmod 644 "$CERT_DIR/server.crt"
echo "   Certificado: $CERT_DIR/server.crt"
echo "   Clave:       $CERT_DIR/server.key"
 
# ── 3. Escribir configuración nginx con HTTPS ─────────────────────
echo "[3/4] Escribiendo configuración nginx..."
cat > "$NGINX_CONF" << EOF
# ── Redirigir HTTP → HTTPS ────────────────────────────────────────
server {
    listen 80;
    server_name _;
    return 301 https://\$host\$request_uri;
}
 
# ── HTTPS ─────────────────────────────────────────────────────────
server {
    listen 443 ssl;
    server_name _;
 
    ssl_certificate     $CERT_DIR/server.crt;
    ssl_certificate_key $CERT_DIR/server.key;
 
    # Protocolos y cifrados seguros
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;
 
    # Cabeceras de seguridad
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-XSS-Protection "1; mode=block" always;
 
    # Frontend estático
    root  /opt/prometheus-target-manager/frontend;
    index index.html;
 
    location / {
        try_files \$uri \$uri/ /index.html;
    }
 
    # Proxy inverso → API Flask
    location /api/ {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Forwarded-For \$remote_addr;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }
 
    location /health {
        proxy_pass http://127.0.0.1:5000;
    }
}
EOF
 
# ── 4. Verificar y recargar nginx ─────────────────────────────────
echo "[4/4] Verificando y recargando nginx..."
nginx -t
systemctl reload nginx
 
echo ""
echo "✅ HTTPS configurado correctamente."
echo ""
echo "   Accede en: https://$IP"
echo ""
echo "⚠  Como el certificado es autofirmado, el navegador mostrará"
echo "   una advertencia de seguridad. Es normal en redes internas."
echo "   Acepta la excepción de seguridad para continuar."
echo ""
echo "   Para evitar la advertencia puedes importar el certificado"
echo "   en los navegadores de tu red:"
echo "   Fichero a distribuir: $CERT_DIR/server.crt"
