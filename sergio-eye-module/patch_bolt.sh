SRC=${SRC:-/odm/etc/eyetracking/runtime/models/Seacliff_V1_5/fbnet/int8/bolt/bolt.ptl}
OUT=${OUT:-/data/local/tmp/bolt_patched.ptl}

STOCK_MD5=e76c2ea88de1e9ff1d7848a2c02ddde8
PATCHED_MD5=499f8b1ab40a24e396a6f0c8d1184414
FILTER_OFF=354347
FILTER_SIZE=4096
BIAS_OFF=358443
BIAS_BYTES='\000\000\000\301\000\000\000\301'

if [ -f "$OUT" ]; then
  md5=$(md5sum "$OUT" 2>/dev/null | cut -d' ' -f1)
  if [ "$md5" = "$PATCHED_MD5" ]; then
    echo "[=] $OUT already patched!"
    exit 0
  fi
fi

if [ ! -f "$SRC" ]; then
  echo "[-] original model not found!: $SRC"
  exit 1
fi

md5=$(md5sum "$SRC" | cut -d' ' -f1)
if [ "$md5" != "$STOCK_MD5" ]; then
  echo "[-] original model md5 mismatch (got $md5, want $STOCK_MD5)."
  echo "OS update might be changed, or the model is incompatible. Might require a different patch"
  exit 1
fi

cp "$SRC" "$OUT" || exit 1

dd if=/dev/zero of="$OUT" bs=1 count=$FILTER_SIZE seek=$FILTER_OFF conv=notrunc status=none || exit 1
printf "$BIAS_BYTES" | dd of="$OUT" bs=1 seek=$BIAS_OFF conv=notrunc status=none || exit 1

md5=$(md5sum "$OUT" | cut -d' ' -f1)
if [ "$md5" != "$PATCHED_MD5" ]; then
  echo "[-] output model md5 mismatch (got $md5, want $PATCHED_MD5)"
  rm -f "$OUT"
  exit 1
fi
echo "[+] model successfully patched!: $OUT ($md5)"
exit 0
