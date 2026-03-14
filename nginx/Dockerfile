FROM nginx:alpine

RUN rm /etc/nginx/nginx.conf
COPY nginx/nginx.conf /etc/nginx/nginx.conf

COPY frontend-viewer /var/www/app

EXPOSE 80
