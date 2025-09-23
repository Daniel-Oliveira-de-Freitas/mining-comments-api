# mining-comments-api (Vercel-ready)

This is a minimal Flask API prepared for Vercel serverless functions.

## Structure
```
/api
  └─ index.py        # Flask app entrypoint
requirements.txt
vercel.json
.vercelignore
```

## Deploy
1. Push this folder to a Git repo and import into Vercel, or zip and upload.
2. Vercel will detect `vercel.json` and deploy the serverless function.
3. Visit `/` to see **Hello Comments World!**
4. POST endpoints:
   - `/comments/scraping`
   - `/comments/crawling`
