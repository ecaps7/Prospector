/**
 * Node resolves import specifiers literally; the app's sources are written for
 * Vite and omit the `.ts` extension. This hook adds it back so `node --test`
 * can import any module under src/ without touching the sources.
 */
const EXTENSIONS = [".ts", ".tsx"];
const HAS_EXTENSION = /\.[cm]?[jt]sx?$|\.json$/;

export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith(".") && !HAS_EXTENSION.test(specifier)) {
    for (const extension of EXTENSIONS) {
      try {
        return await nextResolve(`${specifier}${extension}`, context);
      } catch {
        // try the next candidate, then fall back to the default resolution
      }
    }
  }
  return nextResolve(specifier, context);
}
