export const neutralColors = {
  50: "#FCFDFD",
  100: "#F9FAFB",
  200: "#F4F6F8",
  300: "#DFE3E8",
  400: "#C4CDD5",
  500: "#919EAB",
  600: "#637381",
  700: "#454F5B",
  800: "#1C252E",
  900: "#141A21",
} as const;

export const brandColors = {
  primaryLighter: "#C8FAD6",
  primaryLight: "#5BE49B",
  primary: "#00A76F",
  primaryDark: "#007867",
  primaryDarker: "#004B50",
  secondaryLighter: "#EFD6FF",
  secondaryLight: "#C684FF",
  secondary: "#8E33FF",
  secondaryDark: "#5119B7",
  secondaryDarker: "#27097A",
} as const;

export const feedbackColors = {
  info: "#00B8D9",
  success: "#22C55E",
  warning: "#FFAB00",
  error: "#FF5630",
} as const;

/**
 * China-market convention. Always pair these colors with a sign, arrow, or label.
 * Up is red; down is green.
 */
export const marketColors = {
  up: feedbackColors.error,
  upSoft: "rgb(255 86 48 / 12%)",
  down: feedbackColors.success,
  downSoft: "rgb(34 197 94 / 12%)",
  flat: neutralColors[600],
  flatSoft: "rgb(145 158 171 / 12%)",
} as const;

export const chartColors = {
  primary: brandColors.primaryDark,
  secondary: feedbackColors.info,
  accent: brandColors.secondary,
  warning: feedbackColors.warning,
  comparison: neutralColors[400],
  grid: "rgb(145 158 171 / 20%)",
} as const;

export const shadows = {
  card: "0 0 2px 0 rgb(145 158 171 / 20%), 0 12px 24px -4px rgb(145 158 171 / 12%)",
  cardDark: "0 0 2px 0 rgb(0 0 0 / 20%), 0 12px 24px -4px rgb(0 0 0 / 12%)",
  drawer: "-40px 40px 80px -8px rgb(145 158 171 / 24%)",
  drawerDark: "-40px 40px 80px -8px rgb(0 0 0 / 24%)",
  dialog: "-40px 40px 80px -8px rgb(0 0 0 / 24%)",
  primary: "0 8px 16px rgb(0 167 111 / 24%)",
} as const;

export const componentGeometry = {
  baseRadius: 8,
  cardRadius: 16,
  chipRadius: 6,
  buttonSmallHeight: 32,
  buttonMediumHeight: 36,
  buttonLargeHeight: 48,
  fieldHeight: 56,
  iconButtonSize: 40,
  tabHeight: 42,
  tableHeadHeight: 56,
  tableRowHeight: 76,
  tableDenseRowHeight: 52,
  drawerWidth: 360,
  dialogWidth: 720,
} as const;

export const appLayout = {
  sidebarWidth: 300,
  appBarDesktopHeight: 72,
  appBarMobileHeight: 64,
  contentMaxWidth: 1200,
  analyticsMaxWidth: 1536,
  contentPaddingDesktop: 40,
  contentPaddingMobile: 16,
  gridGap: 24,
} as const;

export const typographyFamilies = {
  sans: '"Public Sans Variable", "PingFang SC", "Microsoft YaHei", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  display:
    'Barlow, "Public Sans Variable", "PingFang SC", "Microsoft YaHei", ui-sans-serif, system-ui, sans-serif',
  mono: '"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
} as const;
