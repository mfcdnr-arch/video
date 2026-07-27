FROM alpine:3.20

RUN apk add --no-cache chrony

COPY chrony.conf /etc/chrony/chrony.conf

EXPOSE 123/udp

# -d — в форграунде (для Docker), -x — не трогать локальные часы,
# только раздавать время клиентам (часы контейнера = часы хоста).
# rm pid-файла обязателен: после docker restart остаётся chronyd.pid со
# значением 1 (chronyd был PID 1), и новый chronyd отказывается стартовать
# («Another chronyd may already be running») — контейнер уходит в рестарт-цикл.
CMD ["sh", "-c", "rm -f /run/chrony/chronyd.pid && exec chronyd -d -x"]
