# Brain Lens Premium Avatar Pool

Goal: build a rotating HeyGen look pool for Brain Lens so videos do not feel like the same presenter every time.

## Safety rule
Use attractive, confident, adult, premium-looking presenters, but keep everything YouTube-safe: no nudity, no lingerie, no sexual pose, no cleavage focus, no minors, no manipulative/pickup framing.

## Recommended avatar looks to create in HeyGen
Create these as separate looks/frames under the Brain Lens avatar group if possible. After each one works in a dry-run, add its exact HeyGen look name to `config/settings.yaml` under `brain_lens.heygen.avatar_names`.

### Look 1: Warm Brunette Studio Expert
Photorealistic portrait of an attractive adult woman, age 28-34, confident and magnetic, stylish but monetization-safe. Modern warm studio with blurred shelves and plants. Shoulder-length brunette hair, natural smile, smart fitted taupe blazer over a simple modest black top, polished but not revealing. Vertical 9:16, medium close-up from chest up, eye-level camera, centered face, soft cinematic light, no watermark, no text.

### Look 2: Bright Blonde Calm Explainer
Photorealistic portrait of an attractive adult woman, age 28-34, charismatic and stylish, premium creator look, alluring but professional and non-explicit. Bright modern office studio, soft daylight, blurred neutral background, plants and minimal decor. Dark-blonde/blonde loose waves, warm confident expression, white blouse with tailored grey blazer, elegant jewelry, natural makeup. Vertical 9:16, medium shot from chest up, face centered, no watermark, no text.

### Look 3: Dark-Hair Podcast Psychology Host
Photorealistic portrait of an attractive adult woman, age 30-38, confident, elegant, magnetic, slightly glamorous but professional and safe. Dark premium podcast-style studio, blurred warm lights, cinematic depth of field. Black or deep-brown hair, poised smile, tasteful navy blazer over a modest top, confident relationship-expert energy. Vertical 9:16, chest-up, eye-level, centered face and shoulders, no watermark, no text.

## Config example after HeyGen upload
```yaml
heygen:
  avatar_rotation_mode: sequential
  avatar_names:
    - "Brain Lens Calm Explainer"
    - "Brain Lens Calm Explainer > Warm Brunette Studio Expert"
    - "Brain Lens Calm Explainer > Bright Blonde Calm Explainer"
    - "Brain Lens Calm Explainer > Dark-Hair Podcast Psychology Host"
```

## Verification rule
Only add a look to production after this passes:
`python run.py build --channel brain_lens --kind short --dry-run`

If a look fails, remove only that look from `avatar_names`; keep the stable base avatar active.
