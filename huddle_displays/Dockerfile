ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8 PYTHONUNBUFFERED=1

RUN apk add --no-cache python3 py3-pip jpeg-dev zlib-dev freetype-dev \
 && python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/
# build-base/musl-dev/python3-dev are only needed to compile Pillow's wheel
# (no prebuilt wheel for this arch/musl-libc combo) - installed as a virtual
# package and removed right after so they don't bloat the final image.
RUN apk add --no-cache --virtual .build-deps build-base musl-dev python3-dev \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && apk del .build-deps

# Inter is the design font. Alpine has no fonts-inter package, so pull the
# release straight from GitHub and keep only the weights the renderer uses.
RUN apk add --no-cache --virtual .fetch curl unzip \
 && mkdir -p /usr/share/fonts/opentype/inter \
 && curl -fsSL -o /tmp/inter.zip \
      https://github.com/rsms/inter/releases/download/v4.0/Inter-4.0.zip \
 && unzip -j -o /tmp/inter.zip 'extras/otf/Inter-Regular.otf' 'extras/otf/Inter-Medium.otf' \
      'extras/otf/Inter-SemiBold.otf' 'extras/otf/Inter-Bold.otf' 'extras/otf/Inter-Black.otf' \
      -d /usr/share/fonts/opentype/inter \
 && rm /tmp/inter.zip \
 && apk del .fetch

COPY app/ /opt/huddle/app/
COPY run.sh /
RUN chmod a+x /run.sh

WORKDIR /opt/huddle
CMD ["/run.sh"]
