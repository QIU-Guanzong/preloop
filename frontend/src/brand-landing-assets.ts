import type { BrandConfig } from './brand-config';

const screenshotDirectory = '/assets/screenshots/quickstart/dark/';
const responsiveScreenshots = new Set(
  [
    'agent_bubble',
    'audit_page',
    'cost_page',
    'dashboard',
    'rules_configured',
  ].map((name) => `${screenshotDirectory}${name}.png`)
);

/** Display derivatives for bundled stills; custom branding and animation pass through. */
export function landingImageSources(original: string): {
  src: string;
  srcset?: string;
  width?: number;
  height?: number;
} {
  if (!responsiveScreenshots.has(original)) return { src: original };
  const stem = original.slice(0, -4);
  return {
    src: `${stem}-800.webp`,
    srcset: `${stem}-800.webp 800w, ${stem}-1600.webp 1600w, ${original} 3200w`,
    width: 3200,
    height: 1900,
  };
}

function isRootRelativeAssetPath(assetPath: string): boolean {
  return (
    assetPath.startsWith('/') &&
    !assetPath.startsWith('//') &&
    !/^https?:/i.test(assetPath)
  );
}

/**
 * Root-relative image paths the landing page will request from ``public/``.
 *
 * Hero and feature placeholders are baked into index.html at build time; a
 * missing file 404s on the live site with a broken-image icon.
 */
export function collectLandingPublicAssetPaths(brand: BrandConfig): string[] {
  const paths: string[] = [];
  const heroImage = brand.landing?.hero?.image;
  if (heroImage) {
    paths.push(heroImage);
  }
  for (const feature of brand.landing?.features || []) {
    if (feature.placeholderImg) {
      paths.push(feature.placeholderImg);
    }
  }
  const displayPaths = paths.flatMap((original) => {
    if (!responsiveScreenshots.has(original)) return [original];
    const stem = original.slice(0, -4);
    return [original, `${stem}-800.webp`, `${stem}-1600.webp`];
  });
  return [...new Set(displayPaths)].filter(isRootRelativeAssetPath);
}
