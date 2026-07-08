FROM alpine:3.20

RUN apk add --no-cache chrony

COPY chrony.conf /etc/chrony/chrony.conf

EXPOSE 123/udp

# -d — в форграунде (для Docker), -x — не трогать локальные часы,
# только раздавать время клиентам (часы контейнера = часы хоста).
CMD ["chronyd", "-d", "-x"]
