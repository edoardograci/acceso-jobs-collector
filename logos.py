"""Download a bounded public logo once per employer; strip metadata and normalize to WebP."""
import base64
from io import BytesIO
from urllib.parse import urlparse
import warnings
import requests
from engine import timestamp, DAY


class LogoCache:
    def __init__(self,known=None,backlog=None):
        self.known=dict(known or {})
        self.attempted=set()
        self.backlog=list(backlog or [])

    def enrich(self,body):
        import time
        from PIL import Image, ImageOps
        uploads=[]
        for item in body['items']+self.backlog:
            company=item.get('companyId','')
            if not company:continue
            saved=self.known.get(company,{})
            if saved.get('path') and timestamp(saved.get('updatedAt'))+30*DAY>time.time():
                item['companyLogo']=saved['path'];continue
            if company in self.attempted or len(self.attempted)>=10:continue
            self.attempted.add(company)
            url=item.get('companyLogo','')
            parsed=urlparse(url)
            if parsed.scheme!='https' or not (parsed.hostname or '').endswith('.licdn.com') or parsed.username or parsed.password or parsed.port:continue
            try:
                with requests.get(url,timeout=15,stream=True,allow_redirects=False) as response:
                    if response.status_code!=200 or not response.headers.get('Content-Type','').startswith('image/'):continue
                    data=bytearray()
                    for chunk in response.iter_content(16384):
                        data.extend(chunk)
                        if len(data)>500000:raise ValueError('Logo too large')
                with warnings.catch_warnings():
                    warnings.simplefilter('error',Image.DecompressionBombWarning)
                    with Image.open(BytesIO(data)) as source:
                        if source.width*source.height>1000000:raise ValueError('Logo dimensions too large')
                        image=ImageOps.exif_transpose(source).convert('RGBA')
                        image.thumbnail((256,256))
                        output=BytesIO();image.save(output,format='WEBP',quality=80,method=4)
                encoded=output.getvalue()
                if len(encoded)>50000:continue
                import hashlib
                path='https://json.acceso.design/jobs/logos/'+hashlib.sha256(encoded).hexdigest()+'.webp'
                uploads.append({'companyId':company,'data':base64.b64encode(encoded).decode()})
                self.known[company]={'path':path,'updatedAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
                item['companyLogo']=path
            except (requests.RequestException,ValueError,OSError,Image.DecompressionBombWarning,Image.DecompressionBombError):
                body['run']['warnings'].append('Company logo unavailable: '+company)
        if uploads:body['logos']=uploads
        return body
