# Viewer style guide

## Typography

Use sentence case or natural title case for user-facing headings, labels, navigation and eyebrow text. Do not write interface copy in all caps or apply CSS uppercase transforms. Preserve the canonical casing of acronyms, protocol identifiers and recorded data.

## Page canvas

Use the subtle gray canvas for collection destinations. Selected experiment, scenario, environment-session and trajectory pages use the white canvas so their contextual navigation and Overview tab have the same visual weight.

## Navigation

The four global destinations are `Overview | Experiments | Sessions | Trajectories`. A selected resource adds exactly one contextual left navigation; never a second permanent drawer. Breadcrumbs carry ownership ancestry and stop at the parent, because the page heading owns the current resource name. The breadcrumb bar keeps a reserved height and renders a same-height skeleton while ancestry loads.
