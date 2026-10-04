declare module "d3-force-3d" {
  interface Force {
    strength(value: number): Force;
  }
  export function forceX(x?: number): Force;
  export function forceY(y?: number): Force;
  export function forceCollide(radius: (node: object) => number): Force;
}
