import zipfile, re, sys

# OHRC
print('=== CH2_OHR_NCP (OHRC Nadir Camera Panchromatic) ===')
zf = 'ch2_ohr_ncp_20260129T1117178849_d_img_d18.zip'
with zipfile.ZipFile(zf) as z:
    xml = z.read('data/calibrated/20260129/ch2_ohr_ncp_20260129T1117178849_d_img_d18.xml').decode('utf-8', errors='ignore')
    for tag in ['processing_level','data_type','isda:pixel_resolution','isda:area',
                'isda:upper_left_latitude','isda:lower_right_latitude',
                'isda:sun_elevation','isda:spacecraft_altitude']:
        m = re.search(f'<{tag}[^>]*>(.*?)</{tag}>', xml, re.IGNORECASE | re.DOTALL)
        if m:
            print(f'  {tag}: {m.group(1).strip()}')
    axes = re.findall(r'<elements>(\d+)</elements>', xml)
    if axes:
        print(f'  lines: {axes[0]}   samples: {axes[1] if len(axes)>1 else "?"}')
    img_size = z.getinfo('data/calibrated/20260129/ch2_ohr_ncp_20260129T1117178849_d_img_d18.img').file_size
    print(f'  img_size: {img_size:,} bytes  ({img_size/1024/1024:.0f} MB)')
    print(f'  pixels (if 16-bit): {img_size//2:,}')

print()
# DFSAR
print('=== CH2O_DFRS (DFSAR — Dual Frequency SAR) ===')
zf2 = 'CH2O_09337_DFRS_DS95_2021_272_00_57.zip'
with zipfile.ZipFile(zf2) as z:
    names = z.namelist()
    print(f'  Files: {len(names)}')
    for n in names:
        info = z.getinfo(n)
        if info.file_size > 0:
            print(f'  {info.filename}  ({info.file_size:,} bytes  {info.file_size/1024/1024:.0f} MB)')
    # Read XML
    xml_files = [n for n in names if n.endswith('.xml')]
    if xml_files:
        try:
            xml = z.read(xml_files[0]).decode('utf-8', errors='ignore')
            print(f'  XML preview ({xml_files[0]}):')
            print(xml[:500])
        except:
            pass
