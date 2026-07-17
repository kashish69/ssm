FROM eclipse-mosquitto:2

# Bake config + ACL into the image so the entrypoint can fix their ownership
# (bind-mounted read-only files can't be chowned). Rebuild picks up edits.
COPY mosquitto.conf /mosquitto/config/mosquitto.conf
COPY acl.conf /mosquitto/config/acl.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
