# Brain Lens Premium Avatar Looks - Upload Manifest

Folder purpose: upload these 10 adult, premium, attractive but YouTube-safe presenter looks into HeyGen.

Recommended HeyGen avatar group name:
Brain Lens Premium Host

Upload/order:
01_warm_brunette_studio_expert.png
02_bright_blonde_calm_explainer.png
03_dark_hair_podcast_psychology_host.png
04_auburn_warm_studio_expert.png
05_black_hair_emerald_podcast_host.png
06_brunette_bob_luxury_apartment_host.png
07_silver_blonde_burgundy_expert.png
08_dark_blonde_soft_glam_creator.png
09_chestnut_glass_office_expert.png
10_curly_teal_therapy_office_host.png

After saving in HeyGen, add the exact HeyGen look names to:
E:\yt_automation\config\settings.yaml

Example:
heygen:
  avatar_rotation_mode: sequential
  avatar_names:
    - "Brain Lens Premium Host > Warm Brunette Studio Expert"
    - "Brain Lens Premium Host > Bright Blonde Calm Explainer"
    - "Brain Lens Premium Host > Dark Hair Podcast Psychology Host"

Important: test each look before enabling all of them:
python run.py build --channel brain_lens --kind short --dry-run
