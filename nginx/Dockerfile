FROM nginx:alpine
RUN apk add --no-cache openssl gettext
COPY entrypoint.sh /entrypoint.sh
COPY ssl-proxy.conf.template /etc/nginx/conf.d/ssl-proxy.conf.template
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
