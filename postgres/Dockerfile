##
# Dockerfile for the PostgreSQL database used by the Stock App.
#
# We extend the official lightweight alpine image and copy an
# initialisation script into the container.  When the database
# container starts for the first time it will run the script to
# create any required tables or seed data.  You should adapt the
# `init.sql` file to suit your application's schema.
##

FROM postgres:15-alpine

# Copy the initialisation SQL script into the directory that the
# official entrypoint monitors.  Files in this directory are run
# exactly once on first database initialisation.
COPY init.sql /docker-entrypoint-initdb.d/